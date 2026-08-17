"""Feed a walk into the engine, and turn its envelope into an exit code.

The generator built the model. This supplies the readings and decides what the answer
means for CI.

**Stage 1 owns presence; only present-and-reading sensors are fed.** That is the
layering rule, and it is a blast-radius choice rather than a workaround: absence is a
question Stage 1 already answers precisely, with a three-valued verdict the engine has
no equivalent for. Feeding an absent sensor would ask the engine to re-derive something
weaker. The engine's own `missing_property` decline stays valuable as the belt to that
brace — if it ever fires, Stage 1 said a sensor was reading and its value did not reach
the model, which is a mapping bug and fails the gate.

**A single walk is one sample, and stuck-at needs about ten.** So liveness warms up:
until enough in-window observations exist, STABILITY declines `insufficient_samples`
and that decline is *reported*, not suppressed. A tool that hid it would look like it
was checking liveness from the first walk, which is the vacuous pass this project keeps
finding in other people's systems.

**Three decline classes, because the vocabulary is not ours.** A decline that asserts
the core case fails the gate; a decline about data sufficiency reports and passes; and
a reason this build does not recognise is reported prominently and does not silently
join either bucket. The engine's reason vocabulary is not exported as a constant, so
this classification is built from reasons actually observed — which makes an unknown
reason a certainty over time, not a hypothetical.

Nothing here imports `arbiter_engine` at module scope. Stage 1 must keep running on a
bench with nothing provisioned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .generator import READING, READING_LOW, Manifest

__all__ = ["FeedResult", "DetectOutcome", "feed", "unmapped_observations",
           "evaluate", "STUCK_AT_SAMPLE_FLOOR"]

# Measured on 0.1.6: a constant series declines below about ten samples and produces a
# STABILITY finding at ten or more. See docs/stage2/s1-threshold-granularity.md for the
# sibling measurement; this one is recorded in the canary.
STUCK_AT_SAMPLE_FLOOR = 10

# Declines that assert the tool's core case. If one of these arrives, something the
# feeder promised the engine did not turn up.
_CORE_CASE_REASONS = frozenset({"missing_property", "no_current_value"})

# Declines that mean "not enough data yet", which is honest and not a failure.
_DATA_SUFFICIENCY_REASONS = frozenset({"insufficient_samples"})


@dataclass
class FeedResult:
    fed: int = 0
    skipped_not_reading: int = 0
    skipped_not_modelled: int = 0
    samples: dict[str, int] = field(default_factory=dict)

    @property
    def warming_up(self) -> dict[str, int]:
        """Sensors with too little history for stuck-at detection, and how much
        they have. Surfaced so a report can say *liveness: warming up, 4/10* rather
        than implying it checked."""
        return {name: n for name, n in self.samples.items()
                if n < STUCK_AT_SAMPLE_FLOOR}


@dataclass
class DetectOutcome:
    findings: list[str] = field(default_factory=list)
    core_case_declines: list[str] = field(default_factory=list)
    data_declines: list[str] = field(default_factory=list)
    unclassified_declines: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    checked: dict = field(default_factory=dict)
    strict: bool = False

    @property
    def exit_code(self) -> int:
        """0 clean, 1 something got worse, 2 could not complete.

        `2` is never returned from here: it belongs to the caller, which knows
        whether the BMC answered and whether the model loaded. Conflating *could
        not evaluate* with *sensors are missing* would fail a good firmware image,
        and it only has to happen once before nobody trusts the gate.
        """
        if self.findings or self.core_case_declines or self.unmapped:
            return 1
        if self.strict and (self.data_declines or self.unclassified_declines):
            return 1
        return 0


def feed(session: Any, manifest: Manifest,
         reports: Sequence[Any]) -> FeedResult:
    """Register entities and history from a chronological run of diff reports.

    `reports` runs oldest to newest; the newest supplies current values and all of
    them supply history. Passing a single report is the normal case and simply means
    liveness has one sample and will say so.
    """
    if not reports:
        return FeedResult()

    result = FeedResult()
    history: dict[str, list[float]] = {}
    for report in reports:
        for match in report.matches:
            if match.live.reading is None:
                continue
            history.setdefault(match.declared.display_name, []).append(
                float(match.live.reading))

    current = reports[-1]
    for match in current.matches:
        name = match.declared.display_name
        entity_type = manifest.type_for(name)
        if entity_type is None:
            # Declared, matched, and deliberately not modelled -- a templated name,
            # a non-sensor Type, or no thresholds to bound against. Counted so the
            # difference between "not checked" and "not modelled" stays visible.
            result.skipped_not_modelled += 1
            continue
        if not match.live.is_reading or match.live.reading is None:
            # Stage 1 owns this verdict. Feeding it would ask the engine to
            # re-derive a weaker version of an answer we already have.
            result.skipped_not_reading += 1
            continue

        value = float(match.live.reading)
        properties = {READING: value}
        sensor = next((s for s in manifest.sensors if s.entity_type == entity_type), None)
        if sensor is not None and sensor.has_lower:
            properties[READING_LOW] = -value

        session.add_entity(entity_type, entity_type, properties=properties)
        series = history.get(name, [])
        if series:
            session.add_observations(entity_type, READING, series,
                                     interval_seconds=60.0)
            if sensor is not None and sensor.has_lower:
                session.add_observations(entity_type, READING_LOW,
                                         [-v for v in series], interval_seconds=60.0)
        result.samples[name] = len(series)
        result.fed += 1
    return result


def unmapped_observations(describe: dict) -> list[dict]:
    """Observations the model never read, from wherever the engine reports them.

    **Measured on 0.1.6: this key is at the TOP LEVEL** of the describe payload, while
    its sibling `unread_fields` sits under `model`. Two introspection keys at two
    levels in one payload, and reading the wrong one returns `None` — which reads as
    *this engine does not support it* rather than *there is nothing to report*.

    So both are checked. If the key relocates, this keeps working and the canary
    fails loudly, which is the right way round: silent blindness here means every
    Redfish-to-model mapping error stops being visible.
    """
    top = describe.get("unmapped_observations") or describe.get("unconsumed_observations")
    nested = (describe.get("model") or {}).get("unconsumed_observations")
    return list(top or nested or [])


def _describe_decline(decline: dict) -> str:
    entity = decline.get("entity_id", "?")
    axiom = decline.get("axiom", "?")
    reason = decline.get("reason", "?")
    detail = decline.get("detail")
    return f"{entity} [{axiom}] {reason}" + (f" -- {detail}" if detail else "")


def evaluate(envelope: dict, describe: dict, manifest: Manifest, *,
             strict_declines: bool = False) -> DetectOutcome:
    """Turn one engine envelope into a verdict a pipeline can act on."""
    outcome = DetectOutcome(checked=envelope.get("checked") or {},
                            strict=strict_declines)

    for finding in envelope.get("findings") or []:
        outcome.findings.append(manifest.translate_finding(finding))

    declines: Iterable[dict] = (envelope.get("not_checked")
                                or envelope.get("declines") or [])
    for decline in declines:
        reason = decline.get("reason")
        rendered = _describe_decline(decline)
        if reason in _CORE_CASE_REASONS:
            # Stage 1 said this sensor was reading. If its value did not reach the
            # model, the mapping is wrong, and a mapping error is invisible unless
            # something fails on it.
            outcome.core_case_declines.append(rendered)
        elif reason in _DATA_SUFFICIENCY_REASONS:
            outcome.data_declines.append(rendered)
        else:
            # Not silently bucketed. A closed vocabulary with a missing member
            # reclassifies the case as its nearest neighbour and reports it with
            # confidence, which is worse than saying so.
            outcome.unclassified_declines.append(rendered)

    for unmapped in unmapped_observations(describe):
        outcome.unmapped.append(
            f"{unmapped.get('entity_id', '?')}.{unmapped.get('property', '?')} "
            f"({unmapped.get('observations', '?')} observations, "
            f"{unmapped.get('reason', 'unread')})")

    return outcome
