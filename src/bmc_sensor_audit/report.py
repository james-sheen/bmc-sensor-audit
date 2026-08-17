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
