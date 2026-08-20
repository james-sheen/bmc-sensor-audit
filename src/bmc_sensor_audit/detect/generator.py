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
`docs/stage2/s1-threshold-granularity.md`, and **re-measured against 0.1.7**, which is
where an expansion plan expected this to have changed. It has not: the engine's own
`axiom_thresholds` module lists BOUNDEDNESS under `OVERRIDE_DECLARED_BUT_UNREACHABLE`
and states that an override never touches a *declared* threshold at all. Type-per-sensor
costs about 3 seconds at a thousand sensors, so the explosion is affordable; it just has
to be recorded, because the type name is sanitised and every finding will name the
sanitised form.

**Lower bounds are declared, not negated.** They used to be: BOUNDEDNESS was
upper-bound-only, so a lower bound became a second indicator carrying the *negated*
reading against negated thresholds, and the report layer un-inverted the text on the way
out. Engine 0.1.7 takes `lower_warning:` and `lower_critical:` directly and says
*is below critical threshold* itself, so the whole mechanism — the mirrored indicator,
the mirrored observations, the translation — is deleted rather than kept working. One
indicator per sensor now carries both bound pairs, and a sensor declaring both gets a
band.

> **If you got here by grepping for `neg`, this is the answer: it is gone.** Every
> match in this file is prose *about* the removal, and a word-level grep counts the
> explanation as if it were the thing. The symbol to search for is **`READING_LOW`**,
> which is absent from the whole package, and the property is pinned by
> `test_no_mirrored_indicator_survives_anywhere` rather than left to a grep.

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
from typing import TYPE_CHECKING

from ..inventory import sensor_types
from ..inventory.entity_manager import ANY_TEMPLATE, Declaration, DeclaredSensor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .supplemental import Supplemental

__all__ = ["GeneratedSensor", "Manifest", "generate", "BOUND_OF_PROBLEM",
           "pairing_candidates", "peer_property", "PEER_PREFIX"]

READING = "reading"
WINDOW = "15m"

# Which side of the band each BOUNDEDNESS finding speaks about, derived by running
# every arm of the axiom on 0.1.7 rather than read off its source: a model declaring
# both pairs, driven above critical, above warning, inside the band, below the lower
# warning, below the lower critical, and ramped in both directions.
#
# It exists because the finding no longer says which bound it means in any field the
# envelope carries. `check()` renders a finding as five keys and drops the `evidence`
# block that holds `bound: upper|lower` -- only `attest()` surfaces that. So the
# problem_type prefix is the discriminator, and a prefix this build has never seen must
# not be silently filed under either side.
BOUND_OF_PROBLEM = {
    "threshold_exceeded": "upper",
    "threshold_warning": "upper",
    "approaching_limit": "upper",
    "below_critical_threshold": "lower",
    "below_warning_threshold": "lower",
    "approaching_floor": "lower",
}

# Levels the engine has a slot for. Anything else is recorded rather than folded.
_MAPPED_LEVELS = ("warning", "critical")

_UNSAFE = re.compile(r"[^0-9A-Za-z]+")

# Prefix for a peer's reading carried on another entity, so a redundancy check has
# both numbers in one place. Distinct from `reading` so an unread peer property shows
# up as itself in the engine's unconsumed-observation report rather than colliding
# with the sensor's own value.
PEER_PREFIX = "peer_"


def peer_property(declared_name: str) -> str:
    """The property key a peer's reading is fed under.

    Sanitised the same way an entity type is, and for the same reason: sensor names
    carry characters a property key should not, and the manifest keeps the mapping
    back to the name on the board.
    """
    base = _UNSAFE.sub("_", declared_name).strip("_") or "peer"
    return f"{PEER_PREFIX}{base}"


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
    lower: tuple[float | None, float | None]        # (warning, critical)
    unmapped_levels: tuple[tuple[str, str, float], ...] = ()   # (bound, level, value)
    agrees_with: tuple[str, ...] = ()               # declared redundant peers
    counter: str | None = None                      # declared cumulative direction
    flow_outputs: tuple[str, ...] = ()              # declared conservation outputs

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
    # Multi-channel parts, offered to an operator and asserted by nobody. See
    # `pairing_candidates` for why these are not pairings.
    candidates: list[dict] = field(default_factory=list)
    supplemental_source: str | None = None

    def exclude(self, reason: str, sensor: DeclaredSensor) -> None:
        self.excluded.setdefault(reason, []).append(sensor.display_name)

    def counts(self) -> dict[str, int]:
        counts = {"generated": len(self.sensors),
                  "with_lower_bound": sum(1 for s in self.sensors if s.has_lower),
                  "unmapped_levels": sum(len(s.unmapped_levels) for s in self.sensors),
                  "anomalies": len(self.anomalies),
                  "redundant_groups": sum(1 for s in self.sensors if s.agrees_with),
                  "counters": sum(1 for s in self.sensors if s.counter),
                  "flows": sum(1 for s in self.sensors if s.flow_outputs),
                  "pairing_candidates": len(self.candidates)}
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
            "supplemental_source": (Path(self.supplemental_source).name
                                    if self.supplemental_source else None),
            "counts": self.counts(),
            "sensors": [{"entity_type": s.entity_type,
                         "declared_name": s.declared_name,
                         "source": Path(s.source).name if s.source else None,
                         "upper": list(s.upper),
                         "lower": list(s.lower),
                         "unmapped_levels": [list(u) for u in s.unmapped_levels],
                         "agrees_with": list(s.agrees_with),
                         "counter": s.counter,
                         "flow_outputs": list(s.flow_outputs)}
                        for s in self.sensors],
            "excluded": {reason: list(names)
                         for reason, names in self.excluded.items()},
            "anomalies": list(self.anomalies),
            "pairing_candidates": list(self.candidates),
        }

    def type_for(self, declared_name: str) -> str | None:
        for sensor in self.sensors:
            if sensor.declared_name == declared_name:
                return sensor.entity_type
        return None

    def translate_finding(self, finding: dict) -> str:
        """Render an engine finding in the sensor's own terms.

        The engine's wording is now correct about the world as well as the model —
        a stopped fan produces *reading is below critical threshold*, not the
        negated-indicator sentence this method used to have to un-invert. What is
        still wrong for a reader is the SUBJECT: every finding names the sanitised
        entity type, and the operator knows the sensor by the name on the board.

        The indicator is the tail of `problem_type` and the bound is its head, both
        parsed rather than assumed. An unrecognised head is reported with the
        engine's own text: a prefix this build has never seen must not be filed
        under `upper` or `lower` on the strength of it being one of the two we know.
        """
        entity_type = self.type_for_entity(finding.get("entity_id", ""))
        problem = str(finding.get("problem_type") or "")
        kind, _, indicator = problem.partition(":")
        severity = finding.get("severity", "")
        reason = str(finding.get("reason") or "")

        sensor = next((s for s in self.sensors if s.entity_type == entity_type), None)
        if sensor is None or not indicator:
            return reason or problem or "unattributable finding"

        side = BOUND_OF_PROBLEM.get(kind)
        if side is None:
            # Not a bound breach -- a frozen series, a redundant disagreement, or
            # whatever the engine grows next. Its own wording is good and
            # self-explaining; the only thing wrong with it is that it names the
            # indicator rather than the sensor.
            text = reason.replace(indicator, "the reading", 1)
            # A redundancy finding also names the PEER, and it names it as the
            # sanitised property key the feeder invented. Left alone, the one finding
            # whose whole value is *these two disagree* would name one sensor an
            # operator recognises and one string that appears nowhere on the board.
            for peer in sensor.agrees_with:
                text = text.replace(peer_property(peer), peer)
            return f"{sensor.declared_name}: {text}"

        bounds = sensor.lower if side == "lower" else sensor.upper
        # `severity` is the engine's vocabulary, not ours: `high` turns up on the two
        # trend arms alongside `warning` and `critical`. Mapping an unknown severity
        # onto the nearest slot printed a real number under the wrong name -- `upper
        # high bound of 3.52` for a reading of 3.35. An unrecognised severity omits
        # the bound instead of asserting one.
        bound = {"critical": bounds[1], "warning": bounds[0]}.get(severity)
        direction = "BELOW its lower" if side == "lower" else "above its upper"
        text = f"{sensor.declared_name} is {direction} {severity} bound"
        return text + (f" of {bound}" if bound is not None else "")

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

        One indicator per sensor since native bounds landed, so this is a lookup
        rather than the un-negation it used to be. It stays because the engine names
        the sanitised entity type and an operator knows the name on the board.
        """
        for sensor in self.sensors:
            if sensor.entity_type == entity_type:
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


def _indicator(upper: tuple[float | None, float | None],
               lower: tuple[float | None, float | None],
               expect_variation: bool) -> dict:
    """One indicator carrying whichever bound pairs the configuration declared.

    A sensor declaring only a ceiling gets `warning`/`critical`; one declaring only
    a floor gets `lower_warning`/`lower_critical`; one declaring both gets a band.
    A key is emitted only when the configuration gave a number for it — writing
    `lower_warning: null` would be this generator inventing a specification the
    machine never made.

    Where a level is declared on one side only, the missing partner takes the
    declared one. That is the pre-existing behaviour and it is deliberate: a bound
    with a critical and no warning should still alarm at critical, and BOUNDEDNESS
    reads both slots.
    """
    indicator: dict = {"name": READING, "type": "NUMERIC",
                       "axioms": ["BOUNDEDNESS", "STABILITY"], "window": WINDOW}
    if upper != (None, None):
        critical = upper[1] if upper[1] is not None else upper[0]
        indicator["warning"] = upper[0] if upper[0] is not None else critical
        indicator["critical"] = critical
    if lower != (None, None):
        low_crit = lower[1] if lower[1] is not None else lower[0]
        indicator["lower_warning"] = lower[0] if lower[0] is not None else low_crit
        indicator["lower_critical"] = low_crit
    if expect_variation:
        indicator["expect_variation"] = True
    return indicator


def pairing_candidates(declaration: Declaration) -> list[dict]:
    """Multi-channel parts, offered as candidates and asserted as nothing.

    A part declaring several channels is where a redundant pair would be if one
    existed, so listing them saves an operator reading every configuration by hand.
    That is the entire claim being made here.

    **It is deliberately not a pairing**, and the two obvious derivations are both
    refuted by the pinned corpus rather than merely doubted:

    * *Same part, several channels.* A TMP421 declares `Name` and `Name1` -- the
      chip's own die and an external diode. On a working board those differ by tens
      of degrees, so pairing them would report a healthy machine as inconsistent.
    * *Same declared thresholds.* `SLED1_THERM_LOCAL` through `SLED6_THERM_LOCAL`
      carry identical bounds and sit on six different parts.

    So the candidate list is a reading aid. What makes two sensors redundant is a
    fact about the hardware, and it arrives from `supplemental.py` with a stated
    basis or it does not arrive.
    """
    grouped: dict[str, list[DeclaredSensor]] = {}
    for sensor in declaration.sensors:
        if sensor.part and sensor.channel is not None:
            grouped.setdefault(sensor.part, []).append(sensor)
    candidates = []
    for part, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        candidates.append({
            "part": part,
            "type": members[0].type,
            "source": Path(members[0].source).name if members[0].source else None,
            "channels": [m.display_name for m in
                         sorted(members, key=lambda m: (m.channel or 0))],
            "note": "channels of one part; redundant only if an operator says so",
        })
    return candidates


def generate(declaration: Declaration, *, domain_id: str = "bmc-sensor-audit",
             expect_variation: bool = True,
             supplemental: "Supplemental | None" = None) -> tuple[dict, Manifest]:
    """Build a domain model and its manifest.

    `expect_variation` turns on stuck-at detection, which is the Stage 2 mission and
    also the setting most likely to produce a false positive on real hardware: a rail
    that genuinely reports an identical value every walk is flat without being broken.
    It defaults on because a liveness tool with liveness off is not one, and it is a
    parameter because the calibration cannot be done without a real capture.
    """
    manifest = Manifest(domain_id=domain_id, expect_variation=expect_variation)
    manifest.candidates = pairing_candidates(declaration)
    modelled_regardless: set[str] = set()
    if supplemental is not None:
        manifest.supplemental_source = supplemental.source
        modelled_regardless = supplemental.modelled_regardless()
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
        if upper == (None, None) and lower == (None, None) \
                and sensor.display_name not in modelled_regardless:
            # Nothing to bound against. Generating an indicator anyway would add an
            # invariant the engine can only decline, inflating the denominator with
            # questions nobody asked.
            #
            # Unless an operator named it in a flow: the Mt.Jade PSU entries declare
            # `pin` and `pout1` with bounds on neither, so this rule would exclude
            # exactly the readings a conservation check needs and leave a declared
            # check that never runs.
            manifest.exclude("no_thresholds", sensor)
            continue

        entity_type = _entity_type(sensor.display_name, taken)
        entity_types.append(entity_type)
        indicator = _indicator(upper, lower, expect_variation)

        group = (supplemental.group_for(sensor.display_name)
                 if supplemental is not None else None)
        counter = (supplemental.counter_for(sensor.display_name)
                   if supplemental is not None else None)
        if group is not None:
            # Peers are named as PROPERTIES on this entity, which is what the engine
            # compares -- a checker holds an IndicatorSpec and an Entity and never
            # the model, so it cannot resolve another entity's indicator. The feeder
            # supplies each peer's reading under the same key.
            indicator["axioms"] = indicator["axioms"] + ["CONSISTENCY"]
            block: dict = {"agrees_with": [peer_property(p) for p in group.peers]}
            if group.tolerance_absolute is not None:
                block["tolerance_absolute"] = group.tolerance_absolute
            elif group.tolerance is not None:
                block["tolerance"] = group.tolerance
            indicator["consistency"] = block
        if counter is not None:
            indicator["axioms"] = indicator["axioms"] + ["MONOTONICITY"]
            indicator["monotonicity"] = {"expected_direction": counter.direction,
                                         "allow_reset": counter.allow_reset}

        flow = (supplemental.flow_for(sensor.display_name)
                if supplemental is not None else None)
        if flow is not None:
            # The input's own reading is the input property; each output arrives as a
            # peer property, the same carriage the redundancy check uses.
            indicator["axioms"] = indicator["axioms"] + ["CONSERVATION"]
            block = {"input_property": READING,
                     "output_properties": [peer_property(o) for o in flow.outputs]}
            if flow.loss_margin is not None:
                block["loss_margin"] = flow.loss_margin
            indicator["conservation"] = block

        if upper == (None, None) and lower == (None, None):
            # Modelled only because a flow or a pairing names it. BOUNDEDNESS with no
            # threshold on either side declines `no_threshold` every pass, which is a
            # decline about the model rather than about the board -- so the axiom is
            # not asked. STABILITY stays: a flow reading that has frozen is still a
            # dead sensor.
            indicator["axioms"] = [a for a in indicator["axioms"]
                                   if a != "BOUNDEDNESS"]

        indicators[entity_type] = [indicator]
        manifest.sensors.append(GeneratedSensor(
            entity_type=entity_type, declared_name=sensor.display_name,
            source=sensor.source, upper=upper, lower=lower,
            unmapped_levels=unmapped,
            agrees_with=tuple(group.peers) if group is not None else (),
            counter=counter.direction if counter is not None else None,
            flow_outputs=tuple(flow.outputs) if flow is not None else ()))

    model = {"domain": {"id": domain_id,
                        "name": "Generated from entity-manager declarations",
                        "entity_types": entity_types,
                        "indicators": indicators}}
    return model, manifest
