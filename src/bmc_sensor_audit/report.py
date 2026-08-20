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

__all__ = ["as_json", "as_text", "KIND_ORDER"]

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


def as_json(report: DiffReport, *, target: str | None = None) -> str:
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
    return json.dumps(payload, indent=2, sort_keys=False)


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
