"""Compare two walks of the same machine: what did the firmware change?

`compare` in this package answers *does the machine match its declaration*. This
answers a different question, and the difference is the whole reason the module
exists: **which sensors did this firmware update remove, rename, or re-threshold?**

Those two are not the same check, and running the first one twice does not
produce the second. A firmware that renames `FAN0` to `Fan 0` still matches the
declaration under the normalised matcher, so a config diff reports nothing while
every dashboard, alert rule and trend query keyed on the old string goes quiet. A
firmware that widens a threshold both walks agree on is invisible for the same
reason. The comparison has to be walk against walk, with the declaration out of it.

**Identity is layered, and the layering is what makes a rename derivable.** A
sensor is paired to its counterpart by NAME first -- the string every downstream
consumer keys on -- and whatever is left is paired by Redfish URI *with the units
and resource type agreeing*, because some firmware numbers sensors positionally
and inserting one shifts every URI after it. A pair that matched that way with two
different names is a rename, stated as one. A sensor whose name and URI both
changed is reported as one removal and one addition, and the report says so rather
than guessing which addition replaced which removal: there is no evidence in two
walks that would settle it, and a wrong guess reads exactly like a right one.

**An incomplete walk withholds absence, on both sides.** The rule `compare` applies
to one walk applies here twice over. A partial *after* walk renders as a firmware
that deleted a chassis full of sensors, which is the most alarming possible way to
report a network timeout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .redfish import LiveSensor, Walk

__all__ = ["Change", "RegressionReport", "compare_walks", "REGRESSION_KINDS"]

# What counts as *worse*, and therefore fails a firmware gate. The split is not
# about how surprising a change is: it is about whether something that worked
# before stops working now. A sensor appearing is news; a sensor vanishing, going
# quiet, changing the name it is keyed under, changing its units, or losing a
# threshold is a downstream consumer breaking.
REGRESSION_KINDS = frozenset({
    "sensor_removed", "sensor_renamed", "reading_lost", "sensor_disabled",
    "threshold_removed", "threshold_moved", "units_changed",
})


@dataclass(frozen=True)
class Change:
    kind: str
    sensor: str
    detail: str
    before_path: str | None = None
    after_path: str | None = None

    @property
    def is_regression(self) -> bool:
        return self.kind in REGRESSION_KINDS

    def __str__(self) -> str:
        return f"[{self.kind}] {self.sensor} -- {self.detail}"


@dataclass
class RegressionReport:
    changes: list[Change] = field(default_factory=list)
    before_count: int = 0
    after_count: int = 0
    paired: int = 0
    complete: bool = True
    absence_withheld: bool = False
    # Whether both walks recorded which properties each object carried. A capture
    # written before that existed carries no such record, so field drift cannot be
    # computed -- and saying nothing drifted would be a claim made on no evidence.
    fields_comparable: bool = False

    @property
    def regressions(self) -> list[Change]:
        return [c for c in self.changes if c.is_regression]

    def counts(self) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        for change in self.changes:
            by_kind[change.kind] = by_kind.get(change.kind, 0) + 1
        return by_kind


def _index(walk: Walk) -> tuple[dict[str, LiveSensor], dict[str, LiveSensor]]:
    """By name and by URI, first occurrence winning in both.

    `setdefault` rather than assignment because a machine can report the same name
    twice -- the deprecated tree and the modern collection both carry it, and a
    walk that read both keeps whichever survived the shape merge. Last-wins would
    silently pair against a different object between the two walks.
    """
    by_name: dict[str, LiveSensor] = {}
    by_path: dict[str, LiveSensor] = {}
    for sensor in walk:
        by_name.setdefault(sensor.name, sensor)
        by_path.setdefault(sensor.path, sensor)
    return by_name, by_path


def _pair(before: Walk, after: Walk) -> tuple[list[tuple[LiveSensor, LiveSensor]],
                                              list[LiveSensor], list[LiveSensor]]:
    before_names, before_paths = _index(before)
    after_names, after_paths = _index(after)

    pairs: list[tuple[LiveSensor, LiveSensor]] = []
    claimed_before: set[int] = set()
    claimed_after: set[int] = set()

    for name, old in before_names.items():
        new = after_names.get(name)
        if new is not None:
            pairs.append((old, new))
            claimed_before.add(id(old))
            claimed_after.add(id(new))

    # Second pass, over what the name pass could not place. A URI that survived
    # while its name changed is the rename case; nothing else in two walks
    # distinguishes it from a coincidence, and the URI is the closest thing
    # Redfish has to a stable identifier for a resource.
    #
    # **A URI on its own is not enough, and this was measured rather than
    # reasoned.** Some implementations number sensors positionally --
    # `/Sensors/s0`, `/Sensors/s1` -- so INSERTING one sensor shifts every URI
    # after it. The first cut of this pass paired on URI alone and reported two
    # confident renames on a firmware that had renamed nothing: it had added a
    # sensor at the front, and every position moved up one.
    #
    # So the URI must be corroborated by something the rename would not have
    # changed. Units and resource type are what a walk carries: a fan is still
    # reported in RPM after it is renamed, while the sensor that merely inherited
    # its position is usually measuring something else entirely. A firmware that
    # renames a sensor AND changes its units in one release is reported as a
    # removal and an addition, which is the honest answer -- there is no longer
    # any evidence tying the two together.
    for path, old in before_paths.items():
        if id(old) in claimed_before:
            continue
        new = after_paths.get(path)
        if new is None or id(new) in claimed_after:
            continue
        if old.units != new.units or old.resource != new.resource:
            continue
        pairs.append((old, new))
        claimed_before.add(id(old))
        claimed_after.add(id(new))

    gone = [s for s in before if id(s) not in claimed_before]
    arrived = [s for s in after if id(s) not in claimed_after]
    return pairs, gone, arrived


def _compare_thresholds(old: LiveSensor, new: LiveSensor, changes: list[Change]) -> None:
    for slot, value in sorted(old.thresholds.items()):
        bound, level = slot
        current = new.thresholds.get(slot)
        if current is None:
            changes.append(Change(
                "threshold_removed", new.name,
                f"the earlier walk carried a {bound} {level} threshold at {value:g}; "
                f"this one carries none. Nothing will alarm on that limit again",
                old.path, new.path))
        elif not _close(current, value):
            changes.append(Change(
                "threshold_moved", new.name,
                f"{bound} {level} was {value:g}, is now {current:g}",
                old.path, new.path))
    for slot, value in sorted(new.thresholds.items()):
        if slot not in old.thresholds:
            bound, level = slot
            changes.append(Change(
                "threshold_added", new.name,
                f"a {bound} {level} threshold at {value:g} appeared, where the "
                f"earlier walk carried none", old.path, new.path))


def _close(a: float, b: float, *, rel: float = 1e-6) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def compare_walks(before: Walk, after: Walk) -> RegressionReport:
    """Diff two walks of one machine, oldest first."""
    report = RegressionReport(before_count=len(before), after_count=len(after),
                              complete=before.complete and after.complete,
                              fields_comparable=before.fields_observed and after.fields_observed)
    changes: list[Change] = []

    pairs, gone, arrived = _pair(before, after)
    report.paired = len(pairs)

    for old, new in pairs:
        if old.name != new.name:
            changes.append(Change(
                "sensor_renamed", new.name,
                f"reported as {old.name!r} in the earlier walk and {new.name!r} in "
                f"this one, at the same URI. Every dashboard, alert rule and trend "
                f"query keyed on the old string stops matching",
                old.path, new.path))
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
        _compare_thresholds(old, new, changes)
        if report.fields_comparable:
            appeared = tuple(n for n in new.undeclared if n not in old.undeclared)
            if appeared:
                noun = "property" if len(appeared) == 1 else "properties"
                changes.append(Change(
                    "field_drift", new.name,
                    f"reports {len(appeared)} {noun} this firmware did not report "
                    f"before and the published schema does not declare: "
                    f"{', '.join(appeared)}", old.path, new.path))

    if report.complete:
        for old in gone:
            changes.append(Change(
                "sensor_removed", old.name,
                f"reported at {old.path} in the earlier walk and not reported at all "
                f"in this one, under any name or URI", old.path, None))
        for new in arrived:
            changes.append(Change(
                "sensor_added", new.name,
                f"reported at {new.path} in this walk and absent from the earlier "
                f"one", None, new.path))
    else:
        # Withheld deliberately, both directions. See the module docstring: a
        # partial walk renders as a firmware that deleted the machine.
        report.absence_withheld = True
        incomplete = "the earlier" if not before.complete else "this"
        if not before.complete and not after.complete:
            incomplete = "both"
        changes.append(Change(
            "walk_incomplete", "(walk)",
            f"{incomplete} walk did not complete, so a sensor missing from it "
            f"cannot be told apart from a subtree that was never read. Sensors "
            f"appearing and disappearing are not reported"))

    lost_shapes = sorted(before.shapes_seen - after.shapes_seen)
    if lost_shapes:
        changes.append(Change(
            "tree_shape_gone", "(chassis)",
            f"the earlier walk found {', '.join(lost_shapes)} and this one does not. "
            f"A client reading only that interface sees nothing at all now, even "
            f"where the sensors themselves are still reported elsewhere"))

    report.changes = changes
    return report
