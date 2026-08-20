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

from .generator import READING, Manifest, peer_property

__all__ = ["FeedResult", "DetectOutcome", "feed", "unmapped_observations",
           "evaluate", "STUCK_AT_SAMPLE_FLOOR", "ENVELOPE_SCHEMA_VERSION"]

# The wire contract this build parses. Versioned separately from the package by the
# engine, deliberately: `meta.schema_version` describes the ENVELOPE shape and moves
# only when that shape changes, so it is not the release number and must not be
# compared against one.
#
# Everything this module knows is keyed to that shape -- `findings` vs `not_checked`,
# the `problem_type` split, `reason` as the decline vocabulary. If the shape moves,
# each of those reads plausibly and wrongly, which is worse than failing: a decline
# the engine renamed lands in `unclassified` and a finding it restructured is quietly
# unattributable. So an unexpected version stops the run rather than degrading it.
ENVELOPE_SCHEMA_VERSION = 1

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
    # Declared redundant pairs where the peer is not currently reading, so agreement
    # was not judged. Reported rather than dropped: a pairing that silently stops
    # being checked looks exactly like a pairing that agrees.
    peers_not_reading: list[str] = field(default_factory=list)
    # The same fact keyed by entity type, which is what a decline names. Kept beside
    # the human-readable list rather than parsed back out of it -- re-deriving one
    # from the other means a display change silently alters a gate decision.
    entities_missing_peers: set[str] = field(default_factory=set)

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
    schema_mismatch: str | None = None

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
    # Current readings by declared name, so a redundant peer's value can be attached
    # to the entity that declares the agreement. Built from the same `is_reading`
    # test the feed loop applies, rather than from `history`, which carries readings
    # from walks where the sensor may since have stopped.
    readings = {m.declared.display_name: float(m.live.reading)
                for m in current.matches
                if m.live.is_reading and m.live.reading is not None}
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

        # A declared redundant peer's reading, carried on this entity so CONSISTENCY
        # has both numbers. Fed only when the peer is itself present and reading:
        # otherwise the engine declines `missing_property`, which for this axiom
        # means *the peer is not there* -- a fact Stage 1 has already reported
        # precisely, and re-deriving it here as a mapping bug would be wrong.
        sensor = next((s for s in manifest.sensors if s.entity_type == entity_type), None)
        carried = () if sensor is None else sensor.agrees_with + sensor.flow_outputs
        for peer in carried:
            peer_value = readings.get(peer)
            if peer_value is None:
                result.peers_not_reading.append(f"{name} -> {peer}")
                result.entities_missing_peers.add(entity_type)
                continue
            properties[peer_property(peer)] = peer_value

        session.add_entity(entity_type, entity_type, properties=properties)
        series = history.get(name, [])
        if series:
            session.add_observations(entity_type, READING, series,
                                     interval_seconds=60.0)
        # CONSERVATION reads a SERIES, not a current value -- fed only the properties
        # it declines `insufficient_samples` with *no observations of input property*,
        # which reads like a warm-up and never clears. CONSISTENCY needs only the
        # current value, so this is redundant for a pairing and harmless: the model
        # declares the property either way, so nothing goes unread.
        if sensor is not None and sensor.flow_outputs:
            for peer in sensor.flow_outputs:
                peer_series = history.get(peer, [])
                if peer_series:
                    session.add_observations(entity_type, peer_property(peer),
                                             peer_series, interval_seconds=60.0)
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


def schema_mismatch(envelope: dict) -> str | None:
    """Say why this envelope cannot be trusted, or nothing if it can.

    Three outcomes rather than two, because *absent* and *different* are not the same
    fact. A version this build does not know is a wire contract that moved. An ABSENT
    version is an engine from before the field existed -- 0.1.6 and earlier shipped
    the same envelope shape without stamping it -- and the pin still admits those, so
    treating a missing stamp as a mismatch would refuse an engine this project
    supports.

    Reading it as `!= 1` alone would have collapsed both into one message and blamed
    the wrong thing for whichever it was.
    """
    meta = envelope.get("meta")
    if not isinstance(meta, dict) or "schema_version" not in meta:
        return None
    version = meta.get("schema_version")
    if version == ENVELOPE_SCHEMA_VERSION:
        return None
    return (f"the engine stamped this envelope schema_version {version!r}; this build "
            f"parses {ENVELOPE_SCHEMA_VERSION}. Every reading below -- findings, "
            f"declines, the sensor a finding names -- is keyed to the shape that "
            f"version describes, so the run is reported as incomplete rather than "
            f"interpreted against a contract that moved")


def _describe_decline(decline: dict) -> str:
    entity = decline.get("entity_id", "?")
    axiom = decline.get("axiom", "?")
    reason = decline.get("reason", "?")
    detail = decline.get("detail")
    return f"{entity} [{axiom}] {reason}" + (f" -- {detail}" if detail else "")


def _is_expected_peer_decline(decline: dict, feed_result: Any) -> bool:
    """Whether a `missing_property` decline is the peer Stage 1 already reported.

    **The decline vocabulary is not one-dimensional, and reading it as though it
    were is a real defect this build had.** `missing_property` under BOUNDEDNESS
    means a value Stage 1 called present never reached the model -- a mapping bug,
    and the reason that reason fails the gate. Under CONSISTENCY it means the peer of
    a declared redundant pair is not carrying a reading, which the feeder already
    knows because it chose not to feed it, and which Stage 1 has already reported
    precisely as absence.

    Classified on `(axiom, reason)` AND cross-checked against what the feeder
    actually did, so a CONSISTENCY `missing_property` for a peer that WAS fed still
    fails the gate -- that one really is a mapping bug. Bucketing on `reason` alone
    failed the gate twice for one absent sensor, the second time asserting the name
    mapping was wrong when it was not.
    """
    if decline.get("axiom") != "CONSISTENCY":
        return False
    unfed = getattr(feed_result, "entities_missing_peers", None) or set()
    return str(decline.get("entity_id") or "") in unfed


def evaluate(envelope: dict, describe: dict, manifest: Manifest, *,
             strict_declines: bool = False, feed_result: Any = None) -> DetectOutcome:
    """Turn one engine envelope into a verdict a pipeline can act on."""
    outcome = DetectOutcome(checked=envelope.get("checked") or {},
                            strict=strict_declines)
    outcome.schema_mismatch = schema_mismatch(envelope)

    for finding in envelope.get("findings") or []:
        outcome.findings.append(manifest.translate_finding(finding))

    declines: Iterable[dict] = (envelope.get("not_checked")
                                or envelope.get("declines") or [])
    for decline in declines:
        reason = decline.get("reason")
        rendered = _describe_decline(decline)
        if reason in _CORE_CASE_REASONS and _is_expected_peer_decline(decline,
                                                                     feed_result):
            # A declared redundant peer that is not reading. Stage 1 has already
            # reported it as absent, precisely; counting it again here would fail the
            # gate twice for one fact and say the mapping was wrong, which it is not.
            outcome.data_declines.append(rendered)
        elif reason in _CORE_CASE_REASONS:
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

    # Peer properties the model DOES declare, inside a `conservation` or
    # `consistency` block. The engine's unconsumed-observation report is built from
    # declared INDICATORS, so a peer reading fed for a cross-signal check arrives
    # there as `undeclared_property` -- and `unmapped` fails the gate, so a healthy
    # board with a declared flow would have exited 1 on every run.
    #
    # Filtered by (entity, property) pair rather than by prefix: a stray `peer_`
    # property on an entity that declares no such peer is still a mapping bug and
    # still reported.
    declared_peers = {
        (sensor.entity_type, peer_property(peer))
        for sensor in manifest.sensors
        for peer in sensor.agrees_with + sensor.flow_outputs}

    for unmapped in unmapped_observations(describe):
        entity = unmapped.get("entity_id", "?")
        prop = unmapped.get("property", "?")
        if (entity, prop) in declared_peers:
            continue
        outcome.unmapped.append(
            f"{entity}.{prop} "
            f"({unmapped.get('observations', '?')} observations, "
            f"{unmapped.get('reason', 'unread')})")

    return outcome
