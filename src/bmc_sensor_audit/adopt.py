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

**THE WRITER NOW LIVES BESIDE THE FORMAT IT WRITES.** Nothing in it was about a
BMC, so `presence-audit` 0.1.13 took it over as `presence_audit.adopt`, where the
keys it sets and the loader that reads them cannot drift apart. What stays here
is what IS this package's: its name in every basis it writes, the flag its
command spells forcing with, and the command itself with the walks it fits
from. Every sentence a file receives is byte for byte what this module wrote
before the move.
"""

from __future__ import annotations

from datetime import datetime

from presence_audit import adopt as _core
from presence_audit.adopt import (ADOPTED_UNTESTED,  # noqa: F401 - re-exported
                                  ADOPTED_WITHOUT_REPLAY_GAIN, SPREAD_KEY,
                                  AdoptionRefused, Proposal, Written,
                                  declared_format, find, format_for_spread,
                                  now, proposals, write)

from . import __version__

#: Who wrote an adopted number: the distribution and its version, which is what
#: a reviewer needs in order to look it up.
BY = f"bmc-sensor-audit {__version__}"

#: How this package's command spells forcing past the replay gate. The core
#: names it in its refusals and in the stamp, so the operator reads back the
#: flag they actually typed.
OVERRIDE = "--force"


def basis_for(proposal: Proposal, *, when: datetime,
              stamp: str | None = None) -> str:
    """The sentence written into `gain_basis`, naming this package."""
    return _core.basis_for(proposal, when=when, by=BY, stamp=stamp,
                           override=OVERRIDE)


def spread_basis_for(proposal: Proposal, *, when: datetime) -> str:
    """The sentence written into `gain_sigma_basis`, naming this package."""
    return _core.spread_basis_for(proposal, when=when, by=BY)


def check(proposal: Proposal, *, force: bool) -> str | None:
    """The replay gate, with this command's flag in its refusals."""
    return _core.check(proposal, force=force, override=OVERRIDE)
