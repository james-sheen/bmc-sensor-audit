"""The field-strictness capability: is this capture carrying properties the
standard does not declare?

Moved out of `report.py`. It reads `resource`, `undeclared` and
`fields_observed` -- three of the members that have no counterpart in the other
bridge built against the same guide -- because it is not part of a presence diff
at all. It is a BMC capability that happened to live beside one.

`unobserved_reason` came with it: every one of its callers is a field-strictness
path, so it was never neutral either.
"""

from __future__ import annotations

import textwrap
from typing import Any

from ..inventory import redfish_schema
from ..inventory.redfish import Walk


def unobserved_reason(walk: Walk) -> str:
    """Why this walk carries no field observations. There are two causes.

    Named separately because the advice differs and a wrong explanation is its own
    defect. A capture written before walks recorded object properties needs
    re-capturing; a walk that could not reach the machine needs the transport
    fixed, and telling its operator to re-capture is telling them to do the thing
    that just failed.
    """
    if not walk.complete:
        return ("the walk did not finish, so no sensor object was read and none "
                "could be compared against the schema; fix the transport and re-run")
    return ("this capture was written before walks recorded object properties, so "
            "it carries no record of what any object reported; re-capture to check it")

def strict_fields_payload(walk: Walk) -> dict[str, Any]:
    """The machine-readable half of the strictness report.

    `checked` is carried as its own key and not implied by an empty `sensors`
    list. A consumer reading only the list cannot otherwise tell a machine with
    nothing undeclared from a capture that never recorded any properties, and the
    two mean opposite things.
    """
    references = redfish_schema.sources()

    payload: dict[str, Any] = {"checked": walk.fields_observed}
    if not walk.fields_observed:
        payload["reason"] = unobserved_reason(walk)
        return payload
    payload["schemas"] = [{"schema": s["schema"], "sha256": s["sha256"]}
                          for s in references]
    payload["objects_checked"] = len(walk)
    payload["sensors"] = [
        {"name": s.name, "resource": s.resource, "path": s.path,
         "undeclared": list(s.undeclared)}
        for s in sorted(walk, key=lambda s: s.name) if s.undeclared
    ]
    return payload

def strict_fields_as_text(walk: Walk, *, target: str) -> str:
    """Name the properties this machine reports that the schema does not declare.

    The early warning that a firmware's Redfish output is wandering from what
    downstream monitoring parses. Property NAMES only -- a sensor object can carry
    `SerialNumber` and `PartNumber`, and printing values would publish the
    machine's identity in the course of complaining about the field.
    """
    lines = ["", f"Field strictness: {target}", "-" * (18 + len(target))]
    if not walk.fields_observed:
        # The one outcome that must never render as a clean board. A capture
        # written before object properties were recorded carries no evidence
        # either way, and printing "nothing undeclared" over it would be a pass
        # asserted on an empty measurement.
        #
        # The prose alone is not enough, and that was reported from outside: this
        # section said NOT CHECKED while the process exited 0, so a pipeline
        # gating on the flag went green with the check never having run. The
        # caller floors the exit at 2 -- see `_report_unobserved_fields`.
        lines.append("  NOT CHECKED -- and this run exits 2, because a check that was")
        lines.append("  asked for and could not run must not report as clean.")
        lines.append(textwrap.fill(unobserved_reason(walk) + ".",
                                   width=74, initial_indent="  ",
                                   subsequent_indent="  "))
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
