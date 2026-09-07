"""Before/after rules only a BMC capture can state.

The regression gate itself is neutral: pair the earlier walk to the later one,
report what changed, refuse to guess when a walk did not complete. These are the
rules that read something the other bridge's capture does not have -- units, an
administrative enabled state, undeclared properties, and the set of interface
shapes a walk found.

`same_point` is the odd one and the most important. Two captures can agree on a
point's address and still not be describing the same point; this domain says so
when the units or the resource differ. A domain with no such evidence says two
points at one address are one point, which is the honest default when nothing
contradicts it.
"""

from __future__ import annotations

from typing import List, Sequence

from ..inventory.redfish import LiveSensor, Walk
from ..inventory.regression import Change


def same_point(old: LiveSensor, new: LiveSensor) -> bool:
    """Whether two captures at one address describe the same point."""
    return old.units == new.units and old.resource == new.resource


def captures_comparable(before: Walk, after: Walk) -> bool:
    """Whether these two captures support this domain's extra comparisons.

    Both walks must have recorded object properties. A capture written before
    that was recorded carries no evidence either way, and comparing against it
    would report *no drift* over an empty measurement.
    """
    return bool(before.fields_observed and after.fields_observed)


def point_changes(old: LiveSensor, new: LiveSensor, *,
                  comparable: bool = False) -> Sequence[Change]:
    """What changed about one point, in terms only this domain has.

    `comparable` is the answer `captures_comparable` gave for the two walks this
    pair came from. The property comparison is skipped without it rather than
    reported as finding nothing.
    """
    changes: List[Change] = []
    if old.units != new.units and (old.units or new.units):
        changes.append(Change(
            "units_changed", new.name,
            f"units were {old.units!r}, are now {new.units!r}",
            old.path, new.path))
    if old.is_enabled and not new.is_enabled:
        changes.append(Change(
            "sensor_disabled", new.name,
            f"was enabled and now reports State={new.state!r}. A disabled sensor "
            f"is typically invisible in the web UI", old.path, new.path))
    elif not old.is_enabled and new.is_enabled:
        changes.append(Change(
            "sensor_enabled", new.name,
            f"was State={old.state!r} and is now enabled", old.path, new.path))
    if old.reading is not None and new.reading is None and new.is_enabled:
        changes.append(Change(
            "reading_lost", new.name,
            f"read {old.reading:g} in the earlier walk and carries no reading in "
            f"this one, while still reporting as enabled", old.path, new.path))
    if comparable:
        appeared = tuple(n for n in new.undeclared if n not in old.undeclared)
        if appeared:
            noun = "property" if len(appeared) == 1 else "properties"
            changes.append(Change(
                "field_drift", new.name,
                f"reports {len(appeared)} {noun} this firmware did not report "
                f"before and the published schema does not declare: "
                f"{', '.join(appeared)}", old.path, new.path))
    return changes


def capture_changes(before: Walk, after: Walk) -> Sequence[Change]:
    """What changed about the capture as a whole."""
    lost_shapes = sorted(before.shapes_seen - after.shapes_seen)
    if not lost_shapes:
        return []
    return [Change(
        "tree_shape_gone", "(chassis)",
        f"the earlier walk found {', '.join(lost_shapes)} and this one does not. "
        f"A client reading only that interface sees nothing at all now, even "
        f"where the sensors themselves are still reported elsewhere")]
