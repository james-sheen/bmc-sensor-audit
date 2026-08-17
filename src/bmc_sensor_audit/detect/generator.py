"""Turn a declaration into an `arbiter-engine` domain model, and say what it left out.

Stage 2 asks the engine a question Stage 1 cannot: *is this sensor still alive?* The
engine answers against a domain model, and nobody is going to hand-write one for a
platform that declares thousands of sensors. This generates it.

**The output is a pair, and the second half is not optional.** A model, and a manifest
recording every declaration that did not become part of it and why. A generator that
silently drops what it cannot express produces a model that looks complete and audits
less than the reader believes — which is the same defect as a coverage tool that counts
PID loops as sensors, one layer along.

Four things this has to get right, each because the obvious version is wrong:

**One entity type per sensor.** Thresholds live on entity *types* in the engine, and
every BMC sensor has its own values. The per-entity override path exists and is not
wired into BOUNDEDNESS, so it cannot be used — measured, see
`docs/stage2/s1-threshold-granularity.md`. Type-per-sensor costs about 3 seconds at a
thousand sensors, so the explosion is affordable; it just has to be recorded, because
the type name is sanitised and every finding will name the sanitised form.

**Lower bounds survive by negation.** BOUNDEDNESS is upper-bound-only by ruling: a
stopped fan reading 0 against a lower critical of 500 produces nothing at all. So a
lower bound becomes a second indicator carrying the negated reading against negated
thresholds, where *below* becomes *above* and the axiom fires correctly. The cost is
that the finding text is inverted for a human, which is why the manifest records the
mapping and the report layer translates rather than the model lying.

**Levels beyond warning and critical are recorded, not folded.** entity-manager
declares `hard_shutdown` and `non_recoverable` as well; the engine has two slots.
Folding a non-recoverable bound into `critical` would move the alarm point to a
different number and call it the same thing. They go in the manifest as unmapped.

**Only sensors are generated.** Non-sensor `Type`s and templated names are excluded
before generation, not filtered afterwards. A `$bus`-name fed to the engine becomes an
entity type nothing can ever match, and the engine has no way to tell you that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..inventory import sensor_types
from ..inventory.entity_manager import ANY_TEMPLATE, Declaration, DeclaredSensor

__all__ = ["GeneratedSensor", "Manifest", "generate"]

READING = "reading"
READING_LOW = "reading_low"
WINDOW = "15m"

# Levels the engine has a slot for. Anything else is recorded rather than folded.
_MAPPED_LEVELS = ("warning", "critical")

_UNSAFE = re.compile(r"[^0-9A-Za-z]+")


def _entity_type(name: str, taken: set[str]) -> str:
    """A sanitised, unique entity-type name.

    Lossy on purpose — the engine's type names are identifiers and sensor names are
    not. The manifest carries the original, because every finding will name this form
    and a reader needs to get back to the sensor on the board.
    """
    base = _UNSAFE.sub("_", name).strip("_") or "sensor"
    if base[0].isdigit():
        base = "s_" + base
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}_{suffix}", suffix + 1
    taken.add(candidate)
    return candidate


@dataclass(frozen=True)
class GeneratedSensor:
    entity_type: str
    declared_name: str
    source: str
    upper: tuple[float | None, float | None]        # (warning, critical)
    lower: tuple[float | None, float | None]        # (warning, critical), un-negated
    unmapped_levels: tuple[tuple[str, str, float], ...] = ()   # (bound, level, value)

    @property
    def has_lower(self) -> bool:
        return any(v is not None for v in self.lower)


@dataclass
class Manifest:
    """What was generated, and everything that was not."""

    domain_id: str
    sensors: list[GeneratedSensor] = field(default_factory=list)
    excluded: dict[str, list[str]] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    expect_variation: bool = True

    def exclude(self, reason: str, sensor: DeclaredSensor) -> None:
        self.excluded.setdefault(reason, []).append(sensor.display_name)

    def counts(self) -> dict[str, int]:
        counts = {"generated": len(self.sensors),
                  "with_lower_bound": sum(1 for s in self.sensors if s.has_lower),
                  "unmapped_levels": sum(len(s.unmapped_levels) for s in self.sensors),
                  "anomalies": len(self.anomalies)}
        for reason, names in self.excluded.items():
            counts[f"excluded_{reason}"] = len(names)
        return counts

    def to_dict(self) -> dict:
        """A serialisable manifest, with source paths reduced to file names.

        **The full path is deliberately not emitted.** A manifest is an artifact
        somebody commits, and the declaration's `source` is an absolute path on the
        machine that generated it, a path under somebody's home directory. The name
        answers the question a reader actually has (which configuration declared this
        sensor); the directory above it only identifies the person who ran the tool.

        Found by running this project's own hygiene rules over generated output, which
        is the whole reason that check exists: a new artifact is a new way for identity
        to escape, and the model and manifest were both new today.
        """
        return {
            "domain_id": self.domain_id,
            "expect_variation": self.expect_variation,
            "counts": self.counts(),
            "sensors": [{"entity_type": s.entity_type,
                         "declared_name": s.declared_name,
                         "source": Path(s.source).name if s.source else None,
                         "upper": list(s.upper),
                         "lower": list(s.lower),
                         "unmapped_levels": [list(u) for u in s.unmapped_levels]}
                        for s in self.sensors],
            "excluded": {reason: list(names)
                         for reason, names in self.excluded.items()},
            "anomalies": list(self.anomalies),
        }

    def type_for(self, declared_name: str) -> str | None:
        for sensor in self.sensors:
            if sensor.declared_name == declared_name:
                return sensor.entity_type
        return None

    def translate_finding(self, finding: dict) -> str:
        """Render an engine finding in the sensor's own terms.

        The engine's own text is correct about the model and wrong about the world:
        a fan that stopped produces **`reading_low exceeds critical threshold`**,
        because the negated indicator really did exceed its negated bound. Reported
        raw that says a stopped fan is spinning too fast.

        The indicator is not a field on the finding. It is the tail of
        `problem_type`, which reads `threshold_exceeded:reading_low` — parsed here
        rather than assumed, because this shape was measured on 0.1.6 and could
        move. An unrecognised shape falls back to the engine's own text rather than
        inventing one.
        """
        entity_type = self.type_for_entity(finding.get("entity_id", ""))
        problem = str(finding.get("problem_type") or "")
        indicator = problem.split(":", 1)[1] if ":" in problem else ""
        severity = finding.get("severity", "")

        sensor = next((s for s in self.sensors if s.entity_type == entity_type), None)
        if sensor is None or not indicator:
            return str(finding.get("reason") or problem or "unattributable finding")

        if indicator == READING_LOW:
            bound = sensor.lower[1] if severity == "critical" else sensor.lower[0]
            return (f"{sensor.declared_name} is BELOW its lower {severity} bound"
                    + (f" of {bound}" if bound is not None else ""))
        bound = sensor.upper[1] if severity == "critical" else sensor.upper[0]
        return (f"{sensor.declared_name} is above its upper {severity} bound"
                + (f" of {bound}" if bound is not None else ""))

    def type_for_entity(self, entity_id: str) -> str | None:
        """The entity type a feeder registered this entity under.

        The feeder names entities after the sensor, so the type is recoverable; this
        exists so the lookup has one home when the feeder lands in Phase 2.
        """
        for sensor in self.sensors:
            if sensor.entity_type == entity_id or sensor.declared_name == entity_id:
                return sensor.entity_type
        return None

    def describe_indicator(self, entity_type: str, indicator: str) -> str:
        """Translate an engine indicator back into what a person measured.

        `reading_low` carries a negated value so an upper-bound axiom can test a lower
        bound. Reported raw it says a fan *exceeded* a threshold while it was actually
        too slow, which is precisely backwards.
        """
        for sensor in self.sensors:
            if sensor.entity_type == entity_type:
                if indicator == READING_LOW:
                    return f"{sensor.declared_name} (below its lower bound)"
                return sensor.declared_name
        return f"{entity_type}.{indicator}"


def _bounds(sensor: DeclaredSensor):
    upper: dict[str, float] = {}
    lower: dict[str, float] = {}
    unmapped: list[tuple[str, str, float]] = []
    for threshold in sensor.thresholds:
        if threshold.bound is None or threshold.level is None:
            continue
        if threshold.level not in _MAPPED_LEVELS:
            unmapped.append((threshold.bound, threshold.level, threshold.value))
            continue
        target = upper if threshold.is_upper else lower
        target[threshold.level] = threshold.value
    return ((upper.get("warning"), upper.get("critical")),
            (lower.get("warning"), lower.get("critical")),
            tuple(unmapped))


def _indicator(name: str, warning: float, critical: float,
               axioms: list[str], expect_variation: bool) -> dict:
    indicator = {"name": name, "type": "NUMERIC", "axioms": axioms,
                 "warning": warning, "critical": critical, "window": WINDOW}
    if expect_variation and "STABILITY" in axioms:
        indicator["expect_variation"] = True
    return indicator


def generate(declaration: Declaration, *, domain_id: str = "bmc-sensor-audit",
             expect_variation: bool = True) -> tuple[dict, Manifest]:
    """Build a domain model and its manifest.

    `expect_variation` turns on stuck-at detection, which is the Stage 2 mission and
    also the setting most likely to produce a false positive on real hardware: a rail
    that genuinely reports an identical value every walk is flat without being broken.
    It defaults on because a liveness tool with liveness off is not one, and it is a
    parameter because the calibration cannot be done without a real capture.
    """
    manifest = Manifest(domain_id=domain_id, expect_variation=expect_variation)
    taken: set[str] = set()
    entity_types: list[str] = []
    indicators: dict[str, list[dict]] = {}

    for anomaly in declaration.anomalies:
        manifest.anomalies.append(f"[{anomaly.kind}] {anomaly.sensor or '(config)'}: "
                                  f"{anomaly.detail}")

    for sensor in declaration.sensors:
        if ANY_TEMPLATE.search(sensor.name):
            # Never fed. A `$bus` name becomes an entity type nothing can match, and
            # the engine cannot tell you that it will never fire.
            manifest.exclude("templated_name", sensor)
            continue
        kind = sensor_types.classify(sensor.type)
        if kind != sensor_types.SENSOR:
            manifest.exclude(kind, sensor)
            continue
        if sensor.disabled_in_config:
            manifest.exclude("disabled_in_config", sensor)
            continue

        upper, lower, unmapped = _bounds(sensor)
        if upper == (None, None) and lower == (None, None):
            # Nothing to bound against. Generating an indicator anyway would add an
            # invariant the engine can only decline, inflating the denominator with
            # questions nobody asked.
            manifest.exclude("no_thresholds", sensor)
            continue

        entity_type = _entity_type(sensor.display_name, taken)
        entity_types.append(entity_type)
        built: list[dict] = []

        if upper != (None, None):
            critical = upper[1] if upper[1] is not None else upper[0]
            warning = upper[0] if upper[0] is not None else critical
            built.append(_indicator(READING, warning, critical,
                                    ["BOUNDEDNESS", "STABILITY"], expect_variation))
        else:
            # Lower-only sensor still needs liveness, and STABILITY has to live
            # somewhere. It rides on the negated indicator below.
            pass

        if lower != (None, None):
            # Negation: below becomes above. `-lower_critical` is the greater of the
            # two negated numbers, which is what BOUNDEDNESS requires of `critical`.
            low_crit = lower[1] if lower[1] is not None else lower[0]
            low_warn = lower[0] if lower[0] is not None else low_crit
            axioms = ["BOUNDEDNESS"] if built else ["BOUNDEDNESS", "STABILITY"]
            built.append(_indicator(READING_LOW, -low_warn, -low_crit,
                                    axioms, expect_variation))

        indicators[entity_type] = built
        manifest.sensors.append(GeneratedSensor(
            entity_type=entity_type, declared_name=sensor.display_name,
            source=sensor.source, upper=upper, lower=lower,
            unmapped_levels=unmapped))

    model = {"domain": {"id": domain_id,
                        "name": "Generated from entity-manager declarations",
                        "entity_types": entity_types,
                        "indicators": indicators}}
    return model, manifest
