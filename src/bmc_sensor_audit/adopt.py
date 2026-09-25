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

**THE SPREAD IS WRITTEN BESIDE THE GAIN.** The engine proposes one with every
fitted gain: the standard error of the fit, which is how well the readings pin
the gain down. Without it, the band around what the coupling projects carries
only the driver's forecast doubt, as though the gain were exact -- and until
supplemental format 3 there was nowhere to write one. It is written with a
basis of its own saying what it assumes, and the file is raised to the oldest
format that carries it: a document under an earlier id is still that document,
and the raise is the notice an older build needs to refuse it by name.
"""

from __future__ import annotations

import json
import math
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
    #: The engine's proposed `gain_sigma`: the standard error of the fitted
    #: gain. `None` where the payload carried none.
    gain_sigma: float | None = None
    #: Whether the engine reports that standard error readable as stated on
    #: this fit. `None` where it did not say.
    sigma_assumes_independence: bool | None = None
    residual_autocorrelation: float | None = None

    @property
    def replay_ran(self) -> bool:
        return self.replay.get("status") == "replayed"

    @property
    def delta(self) -> int | None:
        return self.replay.get("delta") if self.replay_ran else None

    @property
    def spread(self) -> float | None:
        """The standard error, where it can be written down as a spread.

        A zero is not one: the format refuses it because the engine reads zero
        as no spread at all. A merely tiny one -- a noise-free fit proposes
        about 3e-18 -- is that fit's standard error, and is written as it is:
        choosing a floor below which a real number counts as none would be
        this module picking a threshold nobody declared.
        """
        sigma = self.gain_sigma
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            return None
        return sigma


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
        independent = row.get("gain_sigma_assumes_independent_residuals")
        autocorrelation = row.get("residual_autocorrelation")
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
            replay=dict(row.get("replay") or {}),
            gain_sigma=(None if row.get("gain_sigma") is None
                        else float(row["gain_sigma"])),
            sigma_assumes_independence=(None if independent is None
                                        else bool(independent)),
            residual_autocorrelation=(None if autocorrelation is None
                                      else float(autocorrelation))))
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


def spread_basis_for(proposal: Proposal, *, when: datetime) -> str:
    """The sentence written into `gain_sigma_basis`.

    Its own sentence, because the spread is its own claim. The gain's basis
    says where the number came from; this says how sure that number is and
    what the sureness ASSUMES -- a standard error is exact only where the
    fit's residuals are independent, the engine reports whether that held on
    this fit, and nothing in the file can recover that verdict afterwards.
    """
    spread = proposal.spread
    parts = [
        f"adopted_from_proposal {proposal.id} at {when.isoformat()} "
        f"by bmc-sensor-audit {__version__}",
        f"the standard error of the fitted gain, {spread:.6g}, over "
        f"{proposal.n} paired changes through a {proposal.response_model} "
        f"response on a {proposal.grid_seconds:g}s collection grid: how well "
        f"the readings pin the gain down, not how much they scatter",
    ]
    autocorrelation = ("" if proposal.residual_autocorrelation is None else
                       f" (lag-1 residual autocorrelation "
                       f"{proposal.residual_autocorrelation:.3g})")
    if proposal.sigma_assumes_independence is False:
        parts.append(
            f"the engine reports it NOT readable as stated on a "
            f"{proposal.response_model} fit{autocorrelation}: it comes out "
            f"wider than the gain's true scatter, so the band it draws errs "
            f"wide")
    elif proposal.sigma_assumes_independence is True:
        parts.append(
            f"the engine reports it readable as stated on a "
            f"{proposal.response_model} fit{autocorrelation}")
    else:
        parts.append("the engine did not say whether it is readable as stated "
                     "on this fit")
    return ". ".join(parts) + "."


#: The coupling key a spread is written under, which is also the engine's.
SPREAD_KEY = "gain_sigma"


def declared_format(path: str | Path) -> str | None:
    """The format id a supplemental file declares, read as written."""
    return json.loads(Path(path).read_text(encoding="utf-8")).get("format")


def format_for_spread(declared: str | None) -> str | None:
    """The id a file must declare to carry a spread; `None` if it already can.

    The OLDEST such id, not the newest: raising a header is a change to the
    author's file, and the smallest change that carries the key is the one an
    older build is likeliest to read.
    """
    from presence_audit import supplemental as _format

    by_format = getattr(_format, "COUPLING_KEYS_BY_FORMAT", None)
    if by_format is None:
        raise AdoptionRefused(
            "the installed presence-audit reads no supplemental format that "
            "can carry a coupling's spread, so the fitted gain would be "
            "written and never graded. It needs presence-audit 0.1.11 or "
            "later: pip install --upgrade presence-audit")
    if SPREAD_KEY in by_format.get(declared, ()):
        return None
    for name in reversed(_format.ACCEPTED_FORMATS):
        if SPREAD_KEY in by_format.get(name, ()):
            return name
    raise AdoptionRefused(
        "the installed presence-audit declares no format carrying a "
        "coupling's spread")


@dataclass(frozen=True)
class Written:
    """What `write` put into the file, for the command to say."""

    text: str
    spread: float | None
    format_raised_from: str | None = None
    format_raised_to: str | None = None


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


def write(path: str | Path, proposal: Proposal, basis: str,
          spread_basis: str | None = None) -> Written:
    """Set `gain` and `gain_basis` on the matching coupling -- and the spread,
    when `spread_basis` is given and the fit has one -- and prove it parses.

    The file is written, re-read through the format's own loader, and RESTORED
    if that refuses -- this module knows where the keys go and
    `presence-audit` owns what they mean, and a writer carrying its own second
    opinion about a format is how the two come apart.
    """
    from presence_audit.supplemental import SupplementalError, load_supplemental

    path = Path(path)
    original = path.read_text(encoding="utf-8")
    document = json.loads(original)
    spread = proposal.spread if spread_basis else None
    raised_from = raised_to = None
    if spread is not None:
        raised_to = format_for_spread(document.get("format"))
        if raised_to is not None:
            raised_from = document.get("format")

    def _number(into: dict) -> None:
        into["gain"] = proposal.gain
        into["gain_basis"] = basis
        if spread is not None:
            into[SPREAD_KEY] = spread
            into["gain_sigma_basis"] = spread_basis

    written_here = ("gain_basis", SPREAD_KEY, "gain_sigma_basis")
    couplings = document.get("couplings") or []
    for index, block in enumerate(couplings):
        if (str(block.get("from")) == proposal.driver
                and str(block.get("to")) == proposal.driven):
            # Rebuilt rather than mutated, so `gain_basis` lands beside `gain`
            # instead of at the end of the block. A provenance sentence three
            # keys away from the number it is about is one a reviewer reads
            # separately, and the whole reason it is there is to be read with it.
            # The spread follows for the same reason: it is about that number.
            rebuilt: dict = {}
            for key, value in block.items():
                if key == "gain":
                    _number(rebuilt)
                elif key not in written_here:
                    rebuilt[key] = value
            if "gain" not in rebuilt:
                # `gain:` IS OPTIONAL IN THIS FORMAT -- absent means withheld,
                # exactly as `estimate` does. Rebuilding by walking the existing
                # keys would then have written nothing at all and reported
                # success, which is the failure this whole command exists to
                # avoid one level up: a file that reads as adopted and is not.
                _number(rebuilt)
            couplings[index] = rebuilt
            break
    else:
        raise AdoptionRefused(
            f"{path} declares no coupling from {proposal.driver!r} to "
            f"{proposal.driven!r}; it may have been edited since this run "
            f"read it")
    if raised_to is not None:
        document["format"] = raised_to

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
    if spread is not None and getattr(landed, SPREAD_KEY, None) != spread:
        # The same post-condition, for the half that makes the gain gradeable:
        # a spread that did not land leaves a projection that files nothing,
        # from a file that reads as though it would.
        path.write_text(original, encoding="utf-8")
        raise AdoptionRefused(
            f"the file loaded back without the adopted spread on "
            f"{proposal.id}. {path} is unchanged.")
    return Written(text=updated, spread=spread,
                   format_raised_from=raised_from,
                   format_raised_to=raised_to)


def now() -> datetime:
    """A real clock, and deliberately not injectable from the command line.

    The date records when a person adopted this number. A frozen one would make
    every adopted basis claim a time that did not happen, and this project has
    already paid for a frozen `NOW` once -- in a probe, where it silently became
    an expiry date.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)
