"""Write a fitted gain into the supplemental file, with what it was fitted from.

The engine fits a coupling's gain from history and reports it as a PROPOSAL: an `n`, an r-squared, an interval, and a replay saying what
adopting it would have caught. It will not write the number down, and that
refusal is load-bearing -- an engine that replaced a declaration with its own
measurement leaves nobody able to say what the model asserts.

**Somebody still has to write it down**, or every fit is a measurement nobody
can act on. The ruling recorded beside `_proposed_transitions` in the engine
says who: a vertical, in its own file, under a command a person invoked,
recording a basis. That is this module. The distance from the engine is the
whole of the permission -- a separate distribution, a separate command, a named
proposal, and a basis in the file -- and taking any one of them away leaves the
thing the engine refuses.

**THE FILE IS THE AUTHOR'S, AND THIS ONLY EVER FILLS IN A BLANK.** `gain:
estimate` is an author saying *these two are coupled and I do not know by how
much*. Adopting answers that question. A coupling that already declares a
NUMBER is the author's claim about the system, and where the data contradict it
the engine reports a disagreement -- so this refuses to touch it. Overwriting a
declared gain from a fit is the exact act the engine is forbidden to do, one
process further away, and the distance does not make it a different act.

**AND IT REFUSES A NUMBER THAT WOULD NOT HAVE HELPED.** `n` and `r_squared` say
how well a gain fits the window it was fitted on; the replay says whether
adopting it would have caught more of what actually happened. Those come apart,
so the replay is the gate. Two different refusals, because they are two
different facts and their remedies differ:

  * the replay RAN and the proposal caught no more -- `--force` stamps
    `adopted_without_replay_gain`;
  * there was no corpus to replay against at all -- `--force` stamps
    `adopted_untested`.

The phase plan named only the first. Folding the second into it would score a
proposal nobody could test as a proposal that was tested and found useless,
which is the rule the benchmark already applies to a window that never observed
its subject.

**WHAT IS WRITTEN IS RE-READ BEFORE IT IS KEPT.** This module knows where the
keys go; `presence-audit` owns what they mean. So the file is written, loaded
back through `load_supplemental`, and restored if it does not parse. A writer
holding its own second opinion about a format is how the two drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__

#: Stamped into the basis when `--force` overrode a replay that RAN and found
#: the proposal caught no more than the model already did.
ADOPTED_WITHOUT_REPLAY_GAIN = "adopted_without_replay_gain"

#: Stamped when `--force` overrode the absence of a corpus. A different fact
#: from the one above and a different remedy -- file a `surprises.yaml` -- so it
#: is a different stamp. One name for both would tell a later reader that this
#: number was measured against a record and found wanting, when nothing of the
#: sort happened.
ADOPTED_UNTESTED = "adopted_untested"


class AdoptionRefused(Exception):
    """Why a proposal was not written. Every one of these is a refusal, never a
    warning: a run that carried on would leave the operator believing a number
    is in their file."""


@dataclass(frozen=True)
class Proposal:
    """One fitted gain, named the way the operator's own file names it.

    `id` is `<driver> -> <driven>` in DECLARED point names -- the two strings an
    author typed into their supplemental. The engine names its edge after the
    sanitised entity types it generated, which is a spelling nobody chose and
    which changes if the sanitiser does.
    """

    id: str
    driver: str
    driven: str
    gain: float
    n: int
    r_squared: float
    interval: tuple[float, float]
    response_model: str
    grid_seconds: float
    declared_gain: float | None
    replay: dict

    @property
    def replay_ran(self) -> bool:
        return self.replay.get("status") == "replayed"

    @property
    def delta(self) -> int | None:
        return self.replay.get("delta") if self.replay_ran else None


def proposals(described: dict, manifest: Any, *,
              grid_seconds: float) -> list[Proposal]:
    """Every fitted gain in a `model_describe` payload, in the file's names.

    The engine's `edge` is `<source entity id>-><target entity id>`, and this
    bridge registers each entity under its sanitised TYPE. So the mapping back
    is the manifest's, which is the one place the two spellings are recorded
    together -- deriving it here from the sanitiser would be a second copy of a
    naming convention, and the second copy is the one that goes stale.
    """
    by_type = {sensor.entity_type: sensor.declared_name
               for sensor in manifest.sensors}
    out: list[Proposal] = []
    for row in (((described.get("model") or {}).get("proposed_transitions")
                 or {}).get("fitted") or []):
        edge = str(row.get("edge") or "")
        if "->" not in edge:
            continue
        source, target = edge.split("->", 1)
        driver = by_type.get(source)
        driven = by_type.get(target)
        if driver is None or driven is None:
            # The engine fitted an edge this bridge cannot name. Skipped rather
            # than rendered under the sanitised spelling: an id an operator
            # cannot find in their own file is worse than one absent from the
            # list, because they will go looking for the wrong thing.
            continue
        low, high = (row.get("interval") or [float("nan"), float("nan")])[:2]
        out.append(Proposal(
            id=f"{driver} -> {driven}",
            driver=driver, driven=driven,
            gain=float(row["gain"]), n=int(row.get("n") or 0),
            r_squared=float(row.get("r_squared") or 0.0),
            interval=(float(low), float(high)),
            response_model=str(row.get("response_model") or ""),
            grid_seconds=float(grid_seconds),
            declared_gain=(None if row.get("declared_gain") is None
                           else float(row["declared_gain"])),
            replay=dict(row.get("replay") or {})))
    return out


def find(candidates: Sequence[Proposal], wanted: str) -> Proposal:
    """The one proposal named, or a refusal that lists the alternatives.

    Matched on the whole id after collapsing whitespace, so `A->B` and
    `A -> B` are the same request -- a shell and a report render the arrow
    differently and an operator should not have to notice which.
    """
    key = _key(wanted)
    for candidate in candidates:
        if _key(candidate.id) == key:
            return candidate
    if not candidates:
        raise AdoptionRefused(
            "this run fitted no gains, so there is no proposal to adopt. A "
            "coupling is fitted only when both its ends were reading and the "
            "run carried enough paired samples; `detect` reports which.")
    available = "\n".join(f"    {c.id}" for c in candidates)
    raise AdoptionRefused(
        f"no proposal is named {wanted!r}. This run fitted:\n{available}")


def _key(value: str) -> str:
    return "".join(str(value).split()).lower()


def basis_for(proposal: Proposal, *, when: datetime,
              stamp: str | None = None) -> str:
    """The sentence written into `gain_basis`, and it is the whole provenance.

    Long on purpose. `basis` is the field a reviewer reads first, and the
    question they have about an adopted number is not *where did it come from*
    in the abstract -- it is whether they would have adopted it. So the support,
    the grid it was fitted on and what the replay found all travel with it,
    because every one of them changes that answer and none is recoverable from
    the file afterwards.
    """
    low, high = proposal.interval
    parts = [
        f"adopted_from_proposal {proposal.id} at {when.isoformat()} "
        f"by bmc-sensor-audit {__version__}",
        f"fitted {proposal.gain:.6g} through a {proposal.response_model} "
        f"response over {proposal.n} paired changes, r_squared "
        f"{proposal.r_squared:.4g}, interval "
        f"[{low:.6g}, {high:.6g}], on a {proposal.grid_seconds:g}s "
        f"collection grid",
    ]
    if proposal.replay_ran:
        parts.append(
            f"replayed against the {proposal.replay.get('corpus')} corpus: "
            f"detected {proposal.replay.get('detected_before')} -> "
            f"{proposal.replay.get('detected_after')} of "
            f"{proposal.replay.get('confirmed')} confirmed "
            f"(delta {proposal.delta:+d})")
    else:
        parts.append(f"no replay: {proposal.replay.get('reason') or 'unavailable'}")
    if stamp:
        parts.append(
            f"{stamp}: --force was given, so this number is in the file "
            f"without evidence that adopting it catches more")
    return ". ".join(parts) + "."


def check(proposal: Proposal, *, force: bool) -> str | None:
    """The gate. Returns the stamp to record, or raises the refusal.

    `None` means the replay ran and the proposal caught more -- nothing to
    stamp, because the number earned its place.
    """
    if proposal.declared_gain is not None:
        raise AdoptionRefused(
            f"{proposal.id} already declares gain {proposal.declared_gain:g}. "
            f"That is your claim about the machine, and a fit that disagrees "
            f"with it is a FINDING -- `detect` reports the disagreement with "
            f"its interval. Nothing here overwrites a declared number: adopt "
            f"fills in `gain: estimate`, which is the author asking for one.")
    if not proposal.replay_ran:
        if not force:
            raise AdoptionRefused(
                f"{proposal.id} has no corpus to replay against, so nothing "
                f"says whether adopting it would catch more. This is a "
                f"refusal and not a score of zero -- {proposal.replay.get('reason')}"
                f"\n\nEither file a surprises corpus for this domain, or pass "
                f"--force, which writes `{ADOPTED_UNTESTED}` into the basis.")
        return ADOPTED_UNTESTED
    if proposal.delta is not None and proposal.delta <= 0:
        if not force:
            raise AdoptionRefused(
                f"{proposal.id} fits the data well and changes nothing: the "
                f"replay detected {proposal.replay.get('detected_before')} "
                f"before and {proposal.replay.get('detected_after')} after, a "
                f"delta of {proposal.delta:+d} over "
                f"{proposal.replay.get('confirmed')} confirmed entries.\n\n"
                f"r_squared {proposal.r_squared:.4g} says how well the number "
                f"fits the window it was fitted on, which is a different "
                f"question. Pass --force to adopt anyway, which writes "
                f"`{ADOPTED_WITHOUT_REPLAY_GAIN}` into the basis.")
        return ADOPTED_WITHOUT_REPLAY_GAIN
    return None


def write(path: str | Path, proposal: Proposal, basis: str) -> str:
    """Set `gain` and `gain_basis` on the matching coupling, and prove it parses.

    Returns the new text. The file is written, re-read through the format's own
    loader, and RESTORED if that refuses -- this module knows where the keys go
    and `presence-audit` owns what they mean, and a writer carrying its own
    second opinion about a format is how the two come apart.
    """
    from presence_audit.supplemental import SupplementalError, load_supplemental

    path = Path(path)
    original = path.read_text(encoding="utf-8")
    document = json.loads(original)
    couplings = document.get("couplings") or []
    for index, block in enumerate(couplings):
        if (str(block.get("from")) == proposal.driver
                and str(block.get("to")) == proposal.driven):
            # Rebuilt rather than mutated, so `gain_basis` lands beside `gain`
            # instead of at the end of the block. A provenance sentence three
            # keys away from the number it is about is one a reviewer reads
            # separately, and the whole reason it is there is to be read with it.
            rebuilt: dict = {}
            for key, value in block.items():
                if key == "gain":
                    rebuilt["gain"] = proposal.gain
                    rebuilt["gain_basis"] = basis
                elif key != "gain_basis":
                    rebuilt[key] = value
            if "gain" not in rebuilt:
                # `gain:` IS OPTIONAL IN THIS FORMAT -- absent means withheld,
                # exactly as `estimate` does. Rebuilding by walking the existing
                # keys would then have written nothing at all and reported
                # success, which is the failure this whole command exists to
                # avoid one level up: a file that reads as adopted and is not.
                rebuilt["gain"] = proposal.gain
                rebuilt["gain_basis"] = basis
            couplings[index] = rebuilt
            break
    else:
        raise AdoptionRefused(
            f"{path} declares no coupling from {proposal.driver!r} to "
            f"{proposal.driven!r}; it may have been edited since this run "
            f"read it")

    updated = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    path.write_text(updated, encoding="utf-8")
    try:
        reloaded = load_supplemental(str(path))
    except SupplementalError as error:
        path.write_text(original, encoding="utf-8")
        raise AdoptionRefused(
            f"the adopted file did not load back: {error}\n\n"
            f"{path} is unchanged.") from error
    # PARSING IS NOT THE POST-CONDITION. The file the writer left behind could
    # load perfectly and still carry the number it started with; every refusal
    # in this module exists so that a file which reads as adopted IS adopted, and
    # the cheapest way to leave one that is not is to write nothing successfully.
    landed = next((c for c in reloaded.couplings
                   if c.source == proposal.driver and c.target == proposal.driven),
                  None)
    if landed is None or landed.gain_is_withheld or landed.gain != proposal.gain:
        path.write_text(original, encoding="utf-8")
        raise AdoptionRefused(
            f"the file loaded back without the adopted gain on "
            f"{proposal.id}. {path} is unchanged.")
    return updated


def now() -> datetime:
    """A real clock, and deliberately not injectable from the command line.

    The date records when a person adopted this number. A frozen one would make
    every adopted basis claim a time that did not happen, and this project has
    already paid for a frozen `NOW` once -- in a probe, where it silently became
    an expiry date.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)
