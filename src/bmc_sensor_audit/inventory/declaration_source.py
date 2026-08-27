"""Declaration sources that are not the manufacturer's entity-manager files.

**Why this seam exists.** On NVIDIA-managed platforms the GPU and HMC sensors arrive
as runtime self-description -- PLDM PDRs and NSM discovery, projected to Redfish by
the BMC -- and typically have no entity-manager entry at all. Today they land in
coverage's reverse direction, *machine has, declaration doesn't*, which is true and
unhelpful: nothing about them can ever be a regression, because nothing ever
expected them.

So they arrive as an explicit, versioned, LABELED input. The family precedent is to
add declaration sources as formats of their own and never to overwrite the
manufacturer's file:

    entity-manager   the manufacturer's declaration. Wins wherever it declares.
    pdr/1            a reviewed snapshot of one platform's discovered inventory.
    fleet-baseline/1 derived from a fleet. The explicit last resort.

**The circularity hazard is the founding problem of this whole tool, one door
over.** A declaration derived from a walk is only as good as the machine it was
walked from -- and a walk of an unprovisioned board yields an empty declaration that
reads perfectly healthy against every other unprovisioned board. There is no way to
detect that from inside the file, so the file cannot be trusted on its own account.

Two rules follow, and they are the whole of the design:

1. **A candidate refuses to be consumed.** This tool will happily EMIT a `pdr/1`
   from a walk. What it emits carries `reviewed: null`, and a source without a
   complete reviewed marker is refused by `coverage` and `detect` -- loudly, naming
   the file. Adding the marker is the review; it is a person putting their name to
   the claim that this inventory is what the platform should have. Assert versus
   suggest, which is the same rule the supplemental generator follows.

2. **Every run that used one says so.** The provenance line names the format, the
   platform, the firmware, the capture date and the reviewer, and a
   `fleet-baseline/1` additionally says in words that it is a downgrade. Silence
   cannot impersonate the manufacturer.

**Unknown keys are ignored, by the `/1` rule.** A producer may carry whatever else
it needs -- `fleet-sensor-baseline` uses that to record the sensors its cohort
disagreed about, which are precisely the ones it must not declare.

**This format is defined HERE**, in `docs/declaration-sources.md`. It used to say it
was defined by the downstream fleet layer; that layer's own baseline is
`fleet-sensor-baseline/fleet-baseline/2`, a different namespace, and no converter
existed in either direction. Delegating a specification to somebody who did not
know they had it is how a seam gets described twice and built never.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .diff import normalise_name
from .entity_manager import Declaration, DeclaredSensor, Threshold

__all__ = ["DeclarationSource", "DeclarationSourceError", "PDR_FORMAT",
           "FLEET_BASELINE_FORMAT", "SOURCE_PRECEDENCE", "load_declaration_source",
           "merge_sources", "candidate_from_walk"]

PDR_FORMAT = "bmc-sensor-audit/pdr/1"
FLEET_BASELINE_FORMAT = "bmc-sensor-audit/fleet-baseline/1"

#: Precedence, most authoritative first, and PINNED HERE so a test can read it.
#: `entity-manager` is in the list although it does not arrive through this module:
#: leaving it out would make the ordering look like a fact about alternate sources
#: rather than the three-way ruling it is.
SOURCE_PRECEDENCE = ("entity-manager", PDR_FORMAT, FLEET_BASELINE_FORMAT)

#: What a `fleet-baseline/1` says about itself in every report that used it. Held as
#: a constant so the wording is one string rather than one per renderer.
DOWNGRADE_NOTICE = ("derived from a fleet, not from this platform's manufacturer. "
                    "It is the explicit last resort")


class DeclarationSourceError(Exception):
    """A source file that cannot be consumed, and why.

    Raised rather than reported, because there is no degraded mode worth having. A
    declaration source that half-loaded would produce a report whose clean rows and
    absent rows came from different populations, and nothing downstream could tell
    which was which.
    """


@dataclass(frozen=True)
class DeclarationSource:
    """One loaded declaration file, and everything a report has to say about it."""

    kind: str                       # the format string, verbatim
    path: str
    platform: str
    firmware: str | None
    captured_at: str | None
    reviewed_by: str | None
    reviewed_on: str | None
    derived_from: str | None        # fleet-baseline only: what it was derived from
    sensors: tuple[DeclaredSensor, ...] = ()
    #: Names this source actually supplied, after precedence. Set by `merge_sources`.
    supplied: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return SOURCE_PRECEDENCE.index(self.kind)

    @property
    def is_downgrade(self) -> bool:
        return self.kind == FLEET_BASELINE_FORMAT

    def provenance_line(self) -> str:
        """One sentence a reader can act on, printed by every run that used this.

        Names the file as well as the platform. A report quoting only *HGX H100,
        firmware 1.03.05* leaves the reader to find which of four files on the
        runner said so.
        """
        parts = [f"{len(self.supplied)} sensor(s) from {self.kind}",
                 f"platform {self.platform}"]
        if self.firmware:
            parts.append(f"firmware {self.firmware}")
        if self.captured_at:
            parts.append(f"captured {self.captured_at}")
        if self.derived_from:
            parts.append(f"derived from {self.derived_from}")
        parts.append(f"reviewed by {self.reviewed_by} on {self.reviewed_on}")
        line = ", ".join(parts) + f" -- {self.path}"
        if self.is_downgrade:
            line += f"\n    This declaration is {DOWNGRADE_NOTICE}."
        return line


#: Why each provenance field is required, in that field's own words. One shared
#: sentence covering four keys was the first cut, and it produced the `platform`
#: reasoning under a missing `captured_at` -- a message that reads as authoritative
#: and sends the reader to the wrong line of the file.
_REQUIRED_BECAUSE = {
    "platform": ("a declaration scoped to nothing in particular is one nobody can "
                 "tell was pointed at the wrong machine"),
    "firmware": ("discovered inventory moves with firmware, so a snapshot that does "
                 "not say which firmware produced it cannot be checked against "
                 "anything"),
    "captured_at": ("this is a snapshot of one machine at one moment, and a reader "
                    "deciding how much to trust it needs to know which moment"),
    "derived_from": ("a fleet baseline is a downgrade, and what it was derived from "
                     "is the whole of what a reader has to judge it by"),
}


def _string(payload: dict, key: str, where: str, *, required: bool) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        if required:
            raise DeclarationSourceError(
                f"{where} declares no {key!r}. It is required: "
                f"{_REQUIRED_BECAUSE[key]}")
        return None
    if not isinstance(value, str):
        raise DeclarationSourceError(f"{where}: {key!r} is not a string")
    return value


def _thresholds(raw: Any, where: str) -> tuple[Threshold, ...]:
    """`{"upper/critical": 95.0}`, the same slot spelling `walk/1` writes.

    One spelling for the two formats on purpose. A `pdr/1` is normally derived from
    a walk, and a second vocabulary for the same fact is how the two come to
    disagree about which bound `critical` was on.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise DeclarationSourceError(f"{where}: 'thresholds' is not an object")
    out: list[Threshold] = []
    for slot, value in sorted(raw.items()):
        bound, _, level = str(slot).partition("/")
        if bound not in ("upper", "lower") or not level:
            raise DeclarationSourceError(
                f"{where}: threshold slot {slot!r} is not 'upper/<level>' or "
                f"'lower/<level>'")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DeclarationSourceError(
                f"{where}: threshold {slot!r} is not a number")
        out.append(Threshold(
            name=str(slot), value=float(value), severity=None, label=None,
            bound=bound, level=level,
            # entity-manager's own vocabulary, so a downstream reader meets one set
            # of direction words rather than two.
            direction="greater than" if bound == "upper" else "less than"))
    return tuple(out)


def load_declaration_source(path: str | Path) -> DeclarationSource:
    """Read one `pdr/1` or `fleet-baseline/1` file, or raise saying why not."""
    where = str(path)
    try:
        payload = json.loads(Path(path).read_text())
    except OSError as error:
        raise DeclarationSourceError(f"cannot read {where}: {error}") from None
    except json.JSONDecodeError as error:
        raise DeclarationSourceError(
            f"{where} is not parseable as JSON: {error}") from None
    if not isinstance(payload, dict):
        raise DeclarationSourceError(
            f"{where} is a {type(payload).__name__}, not an object")

    declared = payload.get("format")
    if declared not in (PDR_FORMAT, FLEET_BASELINE_FORMAT):
        raise DeclarationSourceError(
            f"{where} declares format {declared!r}. This build consumes "
            f"{PDR_FORMAT!r} and {FLEET_BASELINE_FORMAT!r}")

    platform = _string(payload, "platform", where, required=True)
    # Firmware and capture date are required of a `pdr/1` and not of a
    # `fleet-baseline/1`, because a PDR snapshot IS a claim about one platform at
    # one firmware level -- discovered inventory moves with firmware, and a snapshot
    # that does not say which firmware it came from cannot be checked against
    # anything. A fleet baseline spans firmware levels by construction and says so
    # through `derived_from` instead.
    required = declared == PDR_FORMAT
    firmware = _string(payload, "firmware", where, required=required)
    captured_at = _string(payload, "captured_at", where, required=required)
    derived_from = _string(payload, "derived_from", where,
                           required=declared == FLEET_BASELINE_FORMAT)

    reviewed_by, reviewed_on = _reviewed(payload, where, declared)
    sensors = _sensors(payload, where)
    return DeclarationSource(
        kind=declared, path=where, platform=platform, firmware=firmware,
        captured_at=captured_at, reviewed_by=reviewed_by, reviewed_on=reviewed_on,
        derived_from=derived_from, sensors=sensors)


def _reviewed(payload: dict, where: str, declared: str) -> tuple[str, str]:
    """The gate. A source without a complete reviewed marker is a CANDIDATE.

    **Keyed on the marker's presence, never on a `candidate: true` flag.** A flag
    can be deleted while the review never happens; a marker can only be added by
    someone writing their own name into it. So adding the marker IS the review, and
    there is nothing else to forge.

    Both halves are required. A marker naming a reviewer and no date, or a date and
    no reviewer, is the shape of somebody clearing the gate rather than passing it.
    """
    marker = payload.get("reviewed")
    if marker is None:
        raise DeclarationSourceError(
            f"{where} is a CANDIDATE: it carries no 'reviewed' marker, so nothing "
            f"here has been asserted by anybody.\n"
            f"    A declaration derived from a walk of an unprovisioned board is an "
            f"empty declaration that reads healthy, and no check inside the file can "
            f"tell that from a good one.\n"
            f"    Read it, then add: \"reviewed\": {{\"by\": \"<name>\", \"on\": "
            f"\"<date>\"}}")
    if not isinstance(marker, dict):
        raise DeclarationSourceError(f"{where}: 'reviewed' is not an object")
    by = marker.get("by")
    on = marker.get("on")
    missing = [key for key, value in (("by", by), ("on", on))
               if not isinstance(value, str) or not value]
    if missing:
        raise DeclarationSourceError(
            f"{where}: the 'reviewed' marker is missing {' and '.join(missing)}. "
            f"A half-filled marker is the shape of clearing the gate rather than "
            f"passing it, so it is refused like an absent one")
    return by, on


def _sensors(payload: dict, where: str) -> tuple[DeclaredSensor, ...]:
    raw = payload.get("sensors")
    if not isinstance(raw, list):
        raise DeclarationSourceError(f"{where}: 'sensors' is missing or is not a list")
    if not raw:
        raise DeclarationSourceError(
            f"{where} declares no sensors. An empty declaration reads clean against "
            f"every machine, which is the one answer this tool exists to refuse")
    sensors: list[DeclaredSensor] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        at = f"{where}: sensors[{index}]"
        if not isinstance(entry, dict):
            raise DeclarationSourceError(f"{at} is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise DeclarationSourceError(f"{at} carries no 'name'")
        key = normalise_name(name)
        if key in seen:
            raise DeclarationSourceError(
                f"{at}: {name!r} is declared twice under the matcher's normalisation. "
                f"One of the two would be expected and permanently absent")
        seen.add(key)
        sensors.append(DeclaredSensor(
            name=name, type=entry.get("type"), label=None, record=None, source=where,
            thresholds=_thresholds(entry.get("thresholds"), at),
            # The load-bearing field. These sources record only things that were
            # READING, so the entity-manager `Type` filter must not be applied to
            # them -- every entry would classify as an unrecognised Type and could
            # never fail a gate. See `diff.expects_reading`.
            expects_reading=True))
    return tuple(sensors)


def merge_sources(declaration: Declaration,
                  sources: Iterable[DeclarationSource]) -> Declaration:
    """Layer alternate sources under a manufacturer's declaration, by precedence.

    **The manufacturer wins wherever it declares.** A `pdr/1` covers what
    entity-manager does not, and a `fleet-baseline/1` covers what neither does.
    Nothing here ever replaces an entity-manager entry, and nothing merges two
    entries into one: the loser is dropped whole, so a threshold never arrives from
    a different source than the sensor it bounds.

    Precedence is applied over the MATCHER'S normalisation, not over the raw string.
    Two sources spelling one sensor `HGX_TEMP0` and `HGX TEMP0` are declaring the
    same sensor, and keeping both would expect it twice and report one permanently
    absent -- a false regression created by the merge itself.
    """
    ordered = sorted(sources, key=lambda source: source.rank)
    claimed = {normalise_name(sensor.name) for sensor in declaration.sensors}

    extra: list[DeclaredSensor] = []
    recorded: list[DeclarationSource] = []
    for source in ordered:
        supplied: list[str] = []
        for sensor in source.sensors:
            key = normalise_name(sensor.name)
            if key in claimed:
                continue
            claimed.add(key)
            supplied.append(sensor.name)
            extra.append(sensor)
        # Recorded even when it supplied nothing. A source that was passed and
        # turned out to be entirely redundant is a fact the reader wants: it is
        # usually the sign of a file pointed at the wrong platform.
        recorded.append(replace(source, supplied=tuple(supplied)))

    return Declaration(
        sensors=list(declaration.sensors) + extra,
        anomalies=list(declaration.anomalies),
        unreadable=list(declaration.unreadable),
        files_read=declaration.files_read,
        sources=list(declaration.sources) + recorded)


def candidate_from_walk(walk: Any, *, platform: str, firmware: str | None,
                        source_path: str) -> dict:
    """Derive a `pdr/1` CANDIDATE from a walk. It asserts nothing.

    Written with `reviewed: null`, so the loader refuses it until a person adds
    their name. That refusal is the entire point of the command: the tool can see
    what a machine reports and cannot see whether that machine was provisioned
    correctly, and only the second question makes an inventory an expectation.

    Raises rather than emitting on a walk that cannot support a declaration -- see
    the caller, which checks the same two conditions before it gets here so it can
    say which one and exit 2.
    """
    if not walk.complete:
        raise DeclarationSourceError(
            "this walk did not complete, so a declaration derived from it would bake "
            "the transport failure in as an expectation of fewer sensors")
    if not len(walk):
        raise DeclarationSourceError(
            "this walk reports no sensors, so the declaration derived from it would "
            "be empty -- and an empty declaration reads clean against every machine")
    if not walk.captured_at:
        # **Found by running the emitter and feeding its output back to the loader.**
        # The bletchley fixture predates `captured_at`, so the first version of this
        # wrote a candidate that its own loader refused. `captured_at` is required
        # provenance and this is the only honest way to supply it: stamping it with
        # NOW would date a snapshot to the moment somebody converted it, which is
        # precisely the absent-is-not-a-default rule `walk/1` states one file over.
        raise DeclarationSourceError(
            "this walk carries no capture time, and a pdr/1 must be dated -- a reader "
            "deciding how far to trust a snapshot of one machine needs to know when "
            "it was taken. Re-capture; nothing here will stamp it with now")
    return {
        "format": PDR_FORMAT,
        "platform": platform,
        "firmware": firmware,
        "captured_at": walk.captured_at,
        # Explicitly null rather than absent. The slot is what tells a reader there
        # is a review to do; an absent key just looks like a file that is finished.
        "reviewed": None,
        "note": ("CANDIDATE. Derived from one walk and asserted by nobody. It will "
                 "be refused by coverage and detect until a reviewed marker is "
                 "added. Read it against the platform's documentation first: a walk "
                 "of an unprovisioned board yields an inventory that reads healthy."),
        "derived_from_walk": source_path,
        "sensors": [
            {"name": sensor.name,
             "thresholds": {f"{bound}/{level}": value
                            for (bound, level), value in sorted(sensor.thresholds.items())}}
            for sensor in walk if sensor.is_reading
        ],
    }
