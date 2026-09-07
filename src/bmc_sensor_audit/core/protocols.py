"""What the neutral machinery needs from a capture and from a declaration.

DERIVED FROM TWO BRIDGES AND FROM WHAT THE CODE ACTUALLY REACHES FOR. An earlier
version of this file carried nine members. The modules meant to consume it reach
twenty-three, and the nine were produced by a scan that filtered attribute reads
by variable name -- so they were a description of what I expected rather than a
measurement. The count here is the measured one.

Each member records whether the second bridge -- a discrete-manufacturing audit
built against the same guide by a separate effort -- has a counterpart, because a
protocol is only a protocol if something other than its author can implement it.

**Sixteen have one.** Identity, address, value, whether a point is reading, its
health, the point collections, the capture timestamp, completeness, kind, display
name, sources, exclusion, unreadable entries.

**One diverges in SHAPE**: declared thresholds. Per point here; on entity types
there. Carried as a per-point sequence because that is what the machinery reads,
and a domain that declares them elsewhere resolves them in its adapter.

**Six have no counterpart, and every one of them admits an ABSENT answer** --
`None`, or an empty sequence, or a default that is true where the concept does
not exist. That is what makes a twenty-three member protocol implementable by a
domain that has seventeen of the concepts: the other six are not stubs to fake,
they are questions that domain is entitled to have no answer to.

Satisfaction is proven by EXERCISING an adapter, never by `isinstance`.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Protocol, Sequence, Tuple


class CapturedPoint(Protocol):
    """One named thing that was looked at, and what it read."""

    @property
    def name(self) -> str:
        """The identity the declaration is paired against."""

    @property
    def path(self) -> str:
        """Where it was found. Stable across captures; the pairing key."""

    @property
    def reading(self) -> Optional[float]:
        """Its value, or None when it produced no number.

        None and 0.0 are different answers: a rail reading zero watts is not a
        rail that failed to answer.
        """

    @property
    def is_reading(self) -> bool:
        """Whether it is currently producing a usable value.

        Deliberately not `reading is not None`. One bridge also requires the
        point to be enabled; the other grades sample quality. Both answers
        belong to the bridge.
        """

    @property
    def state(self) -> Optional[str]:
        """Health or condition as the domain reports it, or None."""

    @property
    def thresholds(self) -> Mapping[Tuple[str, str], float]:
        """Limits the MACHINE reported, keyed by (direction, severity).

        NO COUNTERPART in the second bridge: its capture carries readings and
        quality, not limits. Empty is the honest answer there.
        """

    @property
    def units(self) -> Optional[str]:
        """The unit of `reading`, or None.

        NO COUNTERPART. Used only to render a value in a human-readable line, so
        None costs a suffix and nothing else.
        """

    @property
    def is_enabled(self) -> bool:
        """Whether the point is administratively switched on.

        NO COUNTERPART, and the only one of the six that must return a bool. A
        domain where nothing can be switched off answers True -- which is true,
        rather than a stub.
        """


class Capture(Protocol):
    """One pass over whatever the bridge walks."""

    @property
    def points(self) -> Iterable[CapturedPoint]:
        """Every point observed, flattened. May be empty."""

    @property
    def captured_at(self) -> Optional[str]:
        """When the pass ran. The second bridge stamps each sample instead; its
        adapter answers with the newest."""

    @property
    def complete(self) -> bool:
        """Whether the pass finished without losing part of the surface.

        An incomplete capture must never read as an absent point: *we did not
        look* and *it is not there* are different findings, and conflating them
        is the defect a presence audit exists to prevent.
        """

    @property
    def errors(self) -> Sequence[Tuple[str, str]]:
        """What went wrong during the pass, as (where, what).

        NO COUNTERPART found in the second bridge. Empty is honest: a domain
        that records no per-pass errors is not claiming there were none, it is
        declining to answer, and `complete` is where the claim lives.
        """


class DeclaredPoint(Protocol):
    """One thing the declaration says should be there."""

    @property
    def name(self) -> str:
        """The identity a captured point is paired against."""

    @property
    def type(self) -> Optional[str]:
        """What kind of thing it is, in the domain's own vocabulary. The
        classifier reads this; the machinery only passes it along."""

    @property
    def display_name(self) -> str:
        """How to name it in a finding a human reads.

        Separate from `name` because a report that prints the pairing key is a
        report that leaks the pairing key.
        """

    @property
    def source(self) -> str:
        """Which declaration file or authority stated it."""

    @property
    def expects_reading(self) -> Optional[bool]:
        """Whether a value is expected. None means the declaration is silent.

        NO COUNTERPART: the second bridge expects every declared tag to read, so
        its adapter answers None and lets the classifier decide from `type`.
        Three-valued on purpose -- *not declared* is not *declared not to read*.
        """

    @property
    def disabled(self) -> bool:
        """Whether the declaration itself excludes this point from the audit."""

    @property
    def is_templated(self) -> bool:
        """Whether the declared name carries a runtime substitution, so it will
        never match a live name literally. A domain without template syntax
        answers False."""

    @property
    def thresholds(self) -> Sequence[object]:
        """Limits the DECLARATION states for this point.

        DIVERGENT IN SHAPE: per point here, on entity types in the second
        bridge. A domain that declares them elsewhere resolves them per point in
        its adapter, which is the adapter's job and not the protocol's.
        """


class DeclarationSource(Protocol):
    """Whatever the bridge read the expectation from."""

    @property
    def points(self) -> Iterable[DeclaredPoint]:
        """Every declared point. May be empty; empty is a finding, not a pass."""

    @property
    def sources(self) -> Sequence[object]:
        """The files or authorities read, for provenance."""

    @property
    def anomalies(self) -> Sequence[object]:
        """Defects found in the declaration itself, as the domain reports them.

        NO COUNTERPART found. Empty means this domain does not inspect its own
        declaration for contradictions -- a gap worth knowing about, not a
        failure to model.
        """

    @property
    def unreadable(self) -> Sequence[Tuple[str, str]]:
        """Declaration entries that could not be read, as (what, why)."""
