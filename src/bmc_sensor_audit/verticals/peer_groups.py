"""BMC peer grouping: which declared sensors are redundant readings of one thing.

Moved out of the generator. The CONCEPT -- a vertical proposes which declared
points are peers -- is neutral, and any domain might have one. The METHOD is not:
this groups by physical `part` and `channel`, two of the members the other bridge
built against the same guide has no counterpart for.

So the neutral generator asks for peer groups and does not know how they were
found.
"""

from __future__ import annotations

from pathlib import Path

from ..inventory.entity_manager import Declaration, DeclaredSensor


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
