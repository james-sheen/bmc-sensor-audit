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

**An aggregation prefix is DECLARED, never inferred.** A BMC that aggregates a
satellite controller prefixes the resources it republishes, and a prefix that
changes across a firmware or topology change moves every name behind it at once --
which reads here as a mass removal plus a mass addition. `--aggregation-prefix
OLD=NEW` is how an operator states that the two are the same subtree, and the
pairing it produces is annotated so a reader can see the claim it rests on. Nothing
auto-pairs: a prefix map is a claim about topology, and topology claims are
declared. What this module does on its own is *notice* the shape -- a set of names
sharing one leading string vanishing while an identically-shaped set sharing
another appears -- and say so, naming the two prefixes it saw. Surfaced, not
assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .redfish import LiveSensor, Walk

__all__ = ["Change", "RegressionReport", "compare_walks", "parse_prefix_map",
           "REGRESSION_KINDS"]

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
    # How many of `paired` were paired only because the operator declared an
    # aggregation prefix map. Counted separately because it is the one part of the
    # pairing that rests on a claim this module did not verify.
    prefix_paired: int = 0
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


def parse_prefix_map(entries: Sequence[str]) -> list[tuple[str, str]]:
    """Read `OLD=NEW` pairs, or raise `ValueError` naming what was wrong.

    Raises rather than skipping. A prefix map is the operator asserting that two
    subtrees are the same one, and a typo that silently declared nothing would
    produce the full mass-removal report the flag was passed to prevent -- with no
    sign that the flag had not been understood.
    """
    parsed: list[tuple[str, str]] = []
    for entry in entries:
        old, sep, new = entry.partition("=")
        if not sep or not old:
            raise ValueError(
                f"--aggregation-prefix {entry!r} is not OLD=NEW. The old prefix is "
                f"the one in the earlier walk; an empty new prefix is allowed and "
                f"means the prefix was dropped")
        parsed.append((old, new))
    return parsed


def _apply_prefix(name: str, prefix_map: Sequence[tuple[str, str]]) -> str:
    """Rewrite a leading declared prefix. First match wins, longest first.

    **Longest first is load-bearing.** Given `HMC_=GPU_` and `HMC_0_=GPU_0_`, a
    shortest-first pass rewrites `HMC_0_Temp1` with the first entry and produces
    `GPU_0_Temp1` only by coincidence -- and produces the wrong answer as soon as
    the two entries disagree about what follows. Sorting here rather than asking the
    operator to order the flags keeps the map a set of claims rather than a program.
    """
    for old, new in sorted(prefix_map, key=lambda pair: len(pair[0]), reverse=True):
        if name.startswith(old):
            return new + name[len(old):]
    return name


def _pair(before: Walk, after: Walk,
          prefix_map: Sequence[tuple[str, str]] = ()) -> tuple[
              list[tuple[LiveSensor, LiveSensor]], list[LiveSensor], list[LiveSensor],
              list[tuple[LiveSensor, LiveSensor]]]:
    before_names, before_paths = _index(before)
    after_names, after_paths = _index(after)

    pairs: list[tuple[LiveSensor, LiveSensor]] = []
    prefixed: list[tuple[LiveSensor, LiveSensor]] = []
    claimed_before: set[int] = set()
    claimed_after: set[int] = set()

    for name, old in before_names.items():
        new = after_names.get(name)
        if new is not None:
            pairs.append((old, new))
            claimed_before.add(id(old))
            claimed_after.add(id(new))

    # The declared pass, and it sits here for a reason: an operator's claim about
    # topology outranks the positional-URI heuristic below, and cannot outrank an
    # exact name match, which needs no claim at all.
    #
    # **The NAME is rewritten and the URI is not.** The name is the string every
    # dashboard, alert rule and trend query keys on, so it is the field whose change
    # reads as remove-plus-add. Rewriting the URI as well would pair sensors whose
    # names had also changed -- and a name change on top of a prefix change is
    # exactly the case the existing rule refuses to guess at.
    if prefix_map:
        for name, old in before_names.items():
            if id(old) in claimed_before:
                continue
            rewritten = _apply_prefix(name, prefix_map)
            if rewritten == name:
                continue
            new = after_names.get(rewritten)
            if new is None or id(new) in claimed_after:
                continue
            pairs.append((old, new))
            prefixed.append((old, new))
            claimed_before.add(id(old))
            claimed_after.add(id(new))

    # Third pass, over what the name passes could not place. A URI that survived
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
    return pairs, gone, arrived, prefixed


def _common_prefix(names: Sequence[str]) -> str:
    """The longest string every name starts with.

    Computed against the lexicographic extremes, which bound it: any character
    where the smallest and largest names agree is one every name between them
    agrees on too.
    """
    if not names:
        return ""
    first, last = min(names), max(names)
    for index, character in enumerate(first):
        if index >= len(last) or last[index] != character:
            return first[:index]
    return first


def _undeclared_prefix_shift(gone: Sequence[LiveSensor],
                             arrived: Sequence[LiveSensor]) -> Change | None:
    """A whole subtree vanishing while an identically-shaped one appears.

    **Reports and does not pair.** The removals stay removals and the gate still
    fails, which is the honest answer: this module has seen a shape consistent with
    an aggregation prefix change and has no evidence that is what happened. Naming
    the prefix it saw is what turns a wall of removals into something an operator
    can act on in one step -- either by declaring the map or by discovering that a
    controller really did go away.

    **No separator is assumed.** The reported prefix is the measured longest common
    leading string, whatever it turns out to be. Trimming it back to the last
    underscore would be guessing at a convention, and the untrimmed string is both
    true and directly usable as `--aggregation-prefix`.

    **Only when the whole unpaired set shifts together.** Partitioning a mixed set
    of removals into subtrees is the guess this refuses to make, so a genuinely
    removed sensor sitting alongside a shifted subtree suppresses the report. That
    is a miss rather than a wrong answer, and it is the right way round.
    """
    if len(gone) < 2 or len(arrived) < 2:
        # One name changing is a rename, and there is already a pass for that.
        return None
    gone_names = [s.name for s in gone]
    arrived_names = [s.name for s in arrived]
    if len(set(gone_names)) != len(gone_names) or len(set(arrived_names)) != len(
            arrived_names):
        return None

    old_prefix = _common_prefix(gone_names)
    new_prefix = _common_prefix(arrived_names)
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return None
    if {n[len(old_prefix):] for n in gone_names} != {
            n[len(new_prefix):] for n in arrived_names}:
        return None

    return Change(
        "aggregation_prefix_shift", "(topology)",
        f"{len(gone_names)} sensors whose names all begin {old_prefix!r} are gone, "
        f"and {len(arrived_names)} whose names all begin {new_prefix!r} have "
        f"appeared with identical remainders. That is the shape of an aggregation "
        f"prefix change, and it is reported rather than paired: nothing in two "
        f"walks says a satellite was renamed instead of replaced. If that is what "
        f"happened, re-run with --aggregation-prefix {old_prefix}={new_prefix}")


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


def compare_walks(before: Walk, after: Walk, *,
                  prefix_map: Sequence[tuple[str, str]] = ()) -> RegressionReport:
    """Diff two walks of one machine, oldest first.

    `prefix_map` is the operator's declared aggregation-prefix map, `(old, new)`
    pairs. Empty is the normal case and changes nothing.
    """
    report = RegressionReport(before_count=len(before), after_count=len(after),
                              complete=before.complete and after.complete,
                              fields_comparable=before.fields_observed and after.fields_observed)
    changes: list[Change] = []

    pairs, gone, arrived, prefixed = _pair(before, after, prefix_map)
    report.paired = len(pairs)
    report.prefix_paired = len(prefixed)

    renamed_by_prefix = {id(new) for _, new in prefixed}
    for old, new in prefixed:
        # Annotated, not silent. The pairing rests on a claim the operator made and
        # this module cannot check, so the report says which claim and over what.
        changes.append(Change(
            "aggregation_prefix_paired", new.name,
            f"paired with {old.name!r} from the earlier walk through the declared "
            f"prefix map. Everything below about this sensor is judged on that "
            f"claim; nothing here verified it", old.path, new.path))

    for old, new in pairs:
        if old.name != new.name and id(new) not in renamed_by_prefix:
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
        # Emitted before the removals it explains, and it is why this kind sorts
        # first. A reader who meets forty removals and then the note has already
        # started writing the incident.
        shift = _undeclared_prefix_shift(gone, arrived)
        if shift is not None:
            changes.append(shift)
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
