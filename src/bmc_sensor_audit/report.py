"""Render a diff two ways: for a machine, and for a hardware engineer.

Acceptance criterion 4 of Stage 1 is that a before/after run produces a diff a
hardware engineer can read without explanation. That rules out a wall of JSON,
and it rules out a summary that hides which sensor is affected.

The human view leads with the counts, because the first question is always "how
much of this board is fine", and then lists findings grouped by kind with the
most actionable first. The machine view is stable, sorted, and carries the
attribution -- which config declared it, which URI reported it -- because a
finding without a source is a finding nobody can act on.
"""

from __future__ import annotations

import json
from typing import Any

from .inventory.diff import DiffReport
from .inventory.regression import RegressionReport
from .inventory.redfish import Walk

__all__ = ["as_json", "as_text", "KIND_ORDER", "regression_as_text",
           "regression_as_json", "strict_fields_as_text", "strict_fields_payload",
           "supplemental_as_text", "CHANGE_ORDER"]

# Most actionable first. `declared_absent` leads because it is the case that no
# other tool in the stack can produce at all.
KIND_ORDER = (
    "declared_absent",
    "declared_disabled",
    "declared_unreadable",
    "threshold_missing",
    "threshold_drift",
    "threshold_direction_conflict",
    "interface_divergence",
    "unknown_threshold_direction",
    "unclassified_threshold_level",
    "unreadable_threshold_value",
    "malformed_exposes",
    "config_unreadable",
    "walk_incomplete",
    "disabled_in_config_but_live",
    "undeclared_present",
    "matched_inexactly",
)

_HEADLINE = {
    "declared_absent": "Declared and not reported at all",
    "declared_disabled": "Present but switched off",
    "declared_unreadable": "Present and enabled, but not reading",
    "threshold_missing": "Declared threshold absent on the live sensor",
    "threshold_drift": "Threshold moved between config and machine",
    "threshold_direction_conflict": "Config contradicts itself about which side it guards",
    "interface_divergence": "Present on one Redfish interface, absent from the other",
    "unknown_threshold_direction": "Threshold direction not recognised",
    "unclassified_threshold_level": "Threshold severity level not recognised",
    "unreadable_threshold_value": "Threshold value is not a number",
    "malformed_exposes": "Malformed configuration record",
    "config_unreadable": "Configuration file could not be read",
    "walk_incomplete": "The walk did not finish",
    "disabled_in_config_but_live": "Config says disabled; the machine is reporting it",
    "undeclared_present": "Reported by the machine and declared nowhere",
    "matched_inexactly": "Paired by something other than an exact name",
}


def as_json(report: DiffReport, *, target: str | None = None,
            walk: Walk | None = None) -> str:
    payload: dict[str, Any] = {
        "target": target,
        "walk_complete": report.walk_complete,
        "absence_findings_withheld": report.absence_withheld,
        "counts": report.counts(),
        "exit_code": report.exit_code,
        "findings": [
            {"kind": f.kind, "sensor": f.sensor, "detail": f.detail,
             "regression": f.is_regression,
             "declared_in": f.declared_in, "live_path": f.live_path}
            for f in _ordered(report)
        ],
    }
    if walk is not None:
        payload["strict_fields"] = strict_fields_payload(walk)
    return json.dumps(payload, indent=2, sort_keys=False)


def strict_fields_payload(walk: Walk) -> dict[str, Any]:
    """The machine-readable half of the strictness report.

    `checked` is carried as its own key and not implied by an empty `sensors`
    list. A consumer reading only the list cannot otherwise tell a machine with
    nothing undeclared from a capture that never recorded any properties, and the
    two mean opposite things.
    """
    from .inventory import redfish_schema

    payload: dict[str, Any] = {"checked": walk.fields_observed}
    if not walk.fields_observed:
        payload["reason"] = ("this capture predates recording object properties; "
                             "re-capture to check it")
        return payload
    payload["schemas"] = [{"schema": s["schema"], "sha256": s["sha256"]}
                          for s in redfish_schema.sources()]
    payload["objects_checked"] = len(walk)
    payload["sensors"] = [
        {"name": s.name, "resource": s.resource, "path": s.path,
         "undeclared": list(s.undeclared)}
        for s in sorted(walk, key=lambda s: s.name) if s.undeclared
    ]
    return payload


def _ordered(report: DiffReport) -> list:
    rank = {kind: i for i, kind in enumerate(KIND_ORDER)}
    return sorted(report.findings,
                  key=lambda f: (rank.get(f.kind, len(KIND_ORDER)), f.sensor))


def as_text(report: DiffReport, *, target: str | None = None) -> str:
    counts = report.counts()
    lines: list[str] = []
    header = f"Sensor coverage: {target}" if target else "Sensor coverage"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    lines.append(f"  declared          {counts['declared']:>5}")
    lines.append(f"  matched           {counts['matched']:>5}")
    lines.append(f"    reading         {counts['reading']:>5}")
    lines.append(f"    not reading     {counts['present_not_reading']:>5}")
    lines.append(f"  declared, absent  {counts['declared_absent']:>5}")
    lines.append(f"  present, undeclared {counts['undeclared_present']:>3}")

    # Declarations set aside before expectation. Printed even when zero would be
    # wrong -- printed when NON-zero, because an exclusion the reader cannot see is
    # indistinguishable from a checker that forgot to look. The unrecognised line
    # is the one that matters: it is the tool admitting it does not know, and it is
    # how the classification gets corrected.
    if counts.get("not_a_sensor"):
        lines.append(f"  not sensors       {counts['not_a_sensor']:>5}"
                     "   (PID loops, EEPROMs, firmware, muxes -- cannot report a reading)")
    if counts.get("unrecognised_type"):
        lines.append(f"  type unrecognised {counts['unrecognised_type']:>5}"
                     "   (not classified either way; NOT counted as absent)")
        for sensor in report.not_sensor_kinds.get("unrecognised", [])[:10]:
            lines.append(f"      {sensor.display_name}  [{sensor.type}]")
    lines.append("")

    if not report.walk_complete:
        lines.append("  ** THE WALK DID NOT FINISH. Absence findings are withheld,")
        lines.append("     because an unread subtree and a missing sensor look the")
        lines.append("     same from here. Fix the transport and re-run. **")
        lines.append("")

    if not report.findings:
        lines.append("  No findings. Every declared sensor is present and reading,")
        lines.append("  and nothing is reporting that the configuration does not declare.")
        return "\n".join(lines)

    grouped: dict[str, list] = {}
    for finding in _ordered(report):
        grouped.setdefault(finding.kind, []).append(finding)

    for kind, findings in grouped.items():
        title = _HEADLINE.get(kind, kind)
        flag = " (regression)" if findings[0].is_regression else ""
        lines.append(f"{title} -- {len(findings)}{flag}")
        lines.append("-" * len(f"{title} -- {len(findings)}{flag}"))
        for finding in findings:
            lines.append(f"  {finding.sensor}")
            lines.append(f"      {finding.detail}")
            if finding.declared_in:
                lines.append(f"      declared in {finding.declared_in}")
        lines.append("")

    verdict = ("REGRESSIONS PRESENT" if report.regressions
               else "no regressions; findings above are informational")
    lines.append(f"{counts['regressions']} regression(s) of {counts['findings']} "
                 f"finding(s) -- {verdict}")
    return "\n".join(lines)


# Most actionable first, same principle as KIND_ORDER. A removal leads because it
# is the change a firmware release is most often shipped without noticing.
CHANGE_ORDER = (
    "sensor_removed",
    "sensor_renamed",
    "reading_lost",
    "sensor_disabled",
    "threshold_removed",
    "threshold_moved",
    "units_changed",
    "tree_shape_gone",
    "field_drift",
    "walk_incomplete",
    "threshold_added",
    "sensor_enabled",
    "sensor_added",
)

_CHANGE_HEADLINE = {
    "sensor_removed": "Reported before, not reported now",
    "sensor_renamed": "Same URI, different name",
    "reading_lost": "Still enabled, no longer reading",
    "sensor_disabled": "Switched off since the earlier walk",
    "threshold_removed": "Threshold the earlier firmware carried is gone",
    "threshold_moved": "Threshold value changed",
    "units_changed": "Reading units changed",
    "tree_shape_gone": "A Redfish interface stopped being served",
    "field_drift": "New properties the published schema does not declare",
    "walk_incomplete": "A walk did not finish",
    "threshold_added": "A threshold appeared",
    "sensor_enabled": "Switched on since the earlier walk",
    "sensor_added": "Reported now, absent from the earlier walk",
}


def _ordered_changes(report: RegressionReport) -> list:
    rank = {kind: i for i, kind in enumerate(CHANGE_ORDER)}
    return sorted(report.changes,
                  key=lambda c: (rank.get(c.kind, len(CHANGE_ORDER)), c.sensor))


def regression_as_json(report: RegressionReport, *, before: str, after: str) -> str:
    payload: dict[str, Any] = {
        "before": before, "after": after,
        "walks_complete": report.complete,
        "absence_changes_withheld": report.absence_withheld,
        "fields_comparable": report.fields_comparable,
        "sensors_before": report.before_count,
        "sensors_after": report.after_count,
        "paired": report.paired,
        "counts": report.counts(),
        "regressions": len(report.regressions),
        "changes": [
            {"kind": c.kind, "sensor": c.sensor, "detail": c.detail,
             "regression": c.is_regression,
             "before_path": c.before_path, "after_path": c.after_path}
            for c in _ordered_changes(report)
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def regression_as_text(report: RegressionReport, *, before: str, after: str) -> str:
    lines: list[str] = []
    header = "Firmware regression"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    lines.append(f"  before  {before}")
    lines.append(f"  after   {after}")
    lines.append("")
    lines.append(f"  sensors before    {report.before_count:>5}")
    lines.append(f"  sensors after     {report.after_count:>5}")
    lines.append(f"  paired            {report.paired:>5}")

    if not report.complete:
        lines.append("")
        lines.append("  ** A WALK DID NOT FINISH. Sensors appearing and disappearing")
        lines.append("     are not reported, because an unread subtree and a removed")
        lines.append("     sensor look the same from here. **")

    if not report.fields_comparable:
        # Said out loud rather than left as an empty section. One of these captures
        # carries no record of which properties each object had, so field drift was
        # not computed -- which is a different sentence from "nothing drifted".
        lines.append("")
        lines.append("  Field drift not computed: one of these captures was written")
        lines.append("  before walks recorded object properties. Re-capture both to")
        lines.append("  compare them.")
    lines.append("")

    if not report.changes:
        lines.append("  No changes. Every sensor reported before is reported now,")
        lines.append("  under the same name, units, state and thresholds.")
        return "\n".join(lines)

    grouped: dict[str, list] = {}
    for change in _ordered_changes(report):
        grouped.setdefault(change.kind, []).append(change)

    for kind, changes in grouped.items():
        title = _CHANGE_HEADLINE.get(kind, kind)
        flag = " (regression)" if changes[0].is_regression else ""
        lines.append(f"{title} -- {len(changes)}{flag}")
        lines.append("-" * len(f"{title} -- {len(changes)}{flag}"))
        for change in changes:
            lines.append(f"  {change.sensor}")
            lines.append(f"      {change.detail}")
        lines.append("")

    if report.absence_withheld and any(c.kind == "sensor_renamed" for c in report.changes):
        lines.append("  A rename is reported only where the URI stayed the same. A")
        lines.append("  sensor whose name AND URI both changed appears above as one")
        lines.append("  removal and one addition -- but absence is withheld on this")
        lines.append("  run, so neither is shown.")
        lines.append("")
    elif any(c.kind in ("sensor_removed", "sensor_added") for c in report.changes):
        lines.append("  A rename is reported only where the URI stayed the same. A")
        lines.append("  sensor whose name AND URI both changed appears above as one")
        lines.append("  removal and one addition; nothing in two walks says which")
        lines.append("  addition replaced which removal, so this does not guess.")
        lines.append("")

    verdict = ("REGRESSIONS PRESENT" if report.regressions
               else "no regressions; changes above are informational")
    lines.append(f"{len(report.regressions)} regression(s) of {len(report.changes)} "
                 f"change(s) -- {verdict}")
    return "\n".join(lines)


def strict_fields_as_text(walk: Walk, *, target: str) -> str:
    """Name the properties this machine reports that the schema does not declare.

    The early warning that a firmware's Redfish output is wandering from what
    downstream monitoring parses. Property NAMES only -- a sensor object can carry
    `SerialNumber` and `PartNumber`, and printing values would publish the
    machine's identity in the course of complaining about the field.
    """
    from .inventory import redfish_schema

    lines = ["", f"Field strictness: {target}", "-" * (18 + len(target))]
    if not walk.fields_observed:
        # The one outcome that must never render as a clean board. A capture
        # written before object properties were recorded carries no evidence
        # either way, and printing "nothing undeclared" over it would be a pass
        # asserted on an empty measurement.
        lines.append("  NOT CHECKED. This capture was written before walks recorded")
        lines.append("  which properties each object carried, so there is nothing here")
        lines.append("  to compare against the schema. Re-capture to check it.")
        return "\n".join(lines)

    sources = redfish_schema.sources()
    drifting = [s for s in walk if s.undeclared]
    lines.append(f"  {len(walk)} sensor object(s) checked against "
                 f"{', '.join(s['schema'] for s in sources)}")
    if not drifting:
        lines.append("  Every property is one the published schema declares.")
        return "\n".join(lines)

    total = sum(len(s.undeclared) for s in drifting)
    noun = "property" if total == 1 else "properties"
    lines.append(f"  {len(drifting)} sensor(s) carry {total} undeclared {noun}:")
    lines.append("")
    for sensor in sorted(drifting, key=lambda s: s.name):
        lines.append(f"  {sensor.name}  [{sensor.resource}]")
        lines.append(f"      {', '.join(sensor.undeclared)}")
        lines.append(f"      at {sensor.path}")
    lines.append("")
    lines.append("  These are not errors. Redfish provides `Oem` for vendor data and")
    lines.append("  this does not report anything inside it; a property named beside")
    lines.append("  `Reading` is an extension made where the standard offered a place")
    lines.append("  not to make one, and a downstream parser meets it unannounced.")
    return "\n".join(lines)


def supplemental_as_text(supplemental) -> str:
    """What the operator declared, and which numbers they left to the engine.

    **A margin nobody chose is the failure this file exists to prevent, one level
    down.** `basis` is required so a pairing cannot be a guess -- but a group with
    no `tolerance` and a flow with no `loss_margin` are still judged, against the
    engine's own defaults. From the report those are indistinguishable from numbers
    the operator picked, which is exactly the shape of claim the required `basis`
    was added to rule out.

    The default is not quoted here. It belongs to the engine, this build restating
    it is how two copies of one number come to disagree across an engine bump, and
    the sentence that matters -- *this file did not choose it* -- is true whatever
    the number is.
    """
    if supplemental is None or not supplemental:
        return ""
    lines = ["", "Operator declarations", "---------------------"]
    if supplemental.source:
        lines.append(f"  from {supplemental.source}")
    lines.append(f"  redundant groups {len(supplemental.redundant_groups):>4}")
    lines.append(f"  counters         {len(supplemental.counters):>4}")
    lines.append(f"  flows            {len(supplemental.flows):>4}")

    unstated_tolerance = [g for g in supplemental.redundant_groups
                          if g.tolerance is None and g.tolerance_absolute is None]
    unstated_margin = [f for f in supplemental.flows if f.loss_margin is None]
    if unstated_tolerance or unstated_margin:
        lines.append("")
        lines.append("  Judged against a number this file did not choose:")
        for group in unstated_tolerance:
            lines.append(f"      {group.primary} + {', '.join(group.peers)} "
                         f"-- no tolerance declared")
        for flow in unstated_margin:
            lines.append(f"      {flow.input} -> {', '.join(flow.outputs)} "
                         f"-- no loss_margin declared")
        lines.append("  The engine applies its own default for each. That is a")
        lines.append("  working check, not a specified one: declare the number with")
        lines.append("  its basis, or know that the threshold is the engine's.")
    return "\n".join(lines)


def detect_as_text(outcome, feed_result) -> str:
    """Render the Stage 2 verdict.

    Written so the three decline classes stay visibly different. Collapsing them into
    one list would hide the distinction the exit code is built on: a sensor whose value
    never arrived is a defect, a sensor without enough history yet is a fact, and a
    reason this build does not recognise is neither and must not be filed as either.
    """
    lines = ["", "Liveness (Stage 2)", "------------------"]
    if outcome.schema_mismatch:
        # First, and not folded into the finding list. Everything below it was read
        # through a contract this build no longer recognises, so it is reported as
        # unreliable rather than presented as a verdict.
        lines.append("  ** ENVELOPE SCHEMA MISMATCH **")
        lines.append(f"     {outcome.schema_mismatch}")
        lines.append("")
    lines.append(f"  fed to the engine    {feed_result.fed:>5}")
    if feed_result.skipped_not_reading:
        lines.append(f"  not reading, skipped {feed_result.skipped_not_reading:>5}"
                     "   (Stage 1 owns absence; the engine is not asked)")
    if feed_result.skipped_not_modelled:
        lines.append(f"  not modelled         {feed_result.skipped_not_modelled:>5}"
                     "   (templated, non-sensor, or no thresholds to bound against)")
    if outcome.checked:
        lines.append(f"  invariants checked   {outcome.checked.get('invariants', 0):>5}"
                     f"   over {outcome.checked.get('entities', 0)} entities")

    if getattr(feed_result, "peers_not_reading", None):
        # A declared pairing that was not judged, said out loud. Silence here would
        # be the worst available outcome: the operator declared a redundancy check,
        # the report shows no disagreement, and the reason is that nothing was
        # compared rather than that the readings matched.
        lines.append("")
        lines.append(f"  Redundancy not judged -- {len(feed_result.peers_not_reading)} "
                     "declared pair(s) whose peer is not reading:")
        for pair in feed_result.peers_not_reading[:5]:
            lines.append(f"      {pair}")
        if len(feed_result.peers_not_reading) > 5:
            lines.append(f"      ... and {len(feed_result.peers_not_reading) - 5} more")

    warming = feed_result.warming_up
    if warming:
        shown = sorted(warming.items())[:5]
        lines.append("")
        lines.append(f"  Liveness warming up -- {len(warming)} sensor(s) below the "
                     "sample floor; stuck-at cannot be judged yet:")
        for name, count in shown:
            lines.append(f"      {name}: {count} sample(s)")
        if len(warming) > len(shown):
            lines.append(f"      ... and {len(warming) - len(shown)} more")

    if outcome.findings:
        lines.append("")
        lines.append(f"Findings -- {len(outcome.findings)}")
        for finding in outcome.findings:
            lines.append(f"  {finding}")

    if outcome.core_case_declines:
        lines.append("")
        lines.append(f"Could not evaluate, and should have been able to -- "
                     f"{len(outcome.core_case_declines)}")
        lines.append("  Stage 1 reported these as reading. Their values did not reach")
        lines.append("  the model, which means the name mapping is wrong.")
        for decline in outcome.core_case_declines:
            lines.append(f"    {decline}")

    if outcome.unmapped:
        lines.append("")
        lines.append(f"Readings the model never read -- {len(outcome.unmapped)}")
        for entry in outcome.unmapped:
            lines.append(f"    {entry}")

    if outcome.data_declines:
        lines.append("")
        lines.append(f"Not enough data yet -- {len(outcome.data_declines)} "
                     "(reported, not a failure)")
        for decline in outcome.data_declines[:5]:
            lines.append(f"    {decline}")
        if len(outcome.data_declines) > 5:
            lines.append(f"    ... and {len(outcome.data_declines) - 5} more")

    if outcome.unclassified_declines:
        lines.append("")
        lines.append(f"Declines this build does not recognise -- "
                     f"{len(outcome.unclassified_declines)}")
        lines.append("  Reported rather than filed under the nearest known reason.")
        for decline in outcome.unclassified_declines[:5]:
            lines.append(f"    {decline}")

    if not (outcome.findings or outcome.core_case_declines or outcome.unmapped):
        lines.append("")
        if feed_result.fed:
            lines.append("  No liveness findings.")
        else:
            # Not the same sentence, and the difference is the whole point of the
            # section: nothing reached the engine, so this is an absence of evidence
            # and not evidence of health. Reported from outside as reading like a
            # verdict, which it did.
            lines.append("  Liveness not evaluated -- nothing was fed to the engine.")
    return "\n".join(lines)
