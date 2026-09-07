"""What a vertical must supply, and the registry it supplies it through.

The neutral machinery classifies declared points and reports how many fell into
each class. Both of those need a vocabulary, and the vocabulary is the domain:
one bridge's kinds are sensors and not-sensors, another's are assets and
fixtures. So it is supplied, not imported.

**The count keys are part of this, and that is not obvious.** The report a
consumer reads carries per-kind counts under key names taken from the domain. If
the vertical supplied only the classifier, the neutral code would still have to
name the keys, and naming them is the leak. So a vocabulary supplies the names of
its own kinds AND the names they are reported under.

**An empty registry is an error, never a default.** A registry that is quiet when
nothing registered produces a run that classifies nothing and reports cleanly,
which is worse than a crash: everything imports, nothing works, and the first
sign is a wrong answer in somebody's report.
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, Sequence


class PluginError(RuntimeError):
    """A vertical was named and could not be loaded, or registered nothing."""


class VocabularyNotRegistered(RuntimeError):
    """Asked for the vocabulary before any vertical supplied one."""


class Vocabulary(Protocol):
    """The domain half of a presence audit."""

    @property
    def kinds(self) -> Sequence[str]:
        """Every class a declared point can fall into. Must be non-empty."""

    @property
    def count_keys(self) -> Mapping[str, str]:
        """Kind -> the key it is reported under, for the kinds worth reporting.

        A partial mapping is allowed: a kind absent from it is classified and not
        counted separately. That is how the shipped report keeps exactly the keys
        it has always had.
        """

    def classify(self, declared_type: Optional[str]) -> str:
        """Which kind a declared point's type falls into. Must return a member
        of `kinds` -- a classifier that can answer outside its own vocabulary is
        not one."""

    def is_auditable(self, kind: str) -> bool:
        """Whether points of this kind are the ones the audit is about.

        Asked directly rather than inferred from `count_keys`. In this
        distribution's own vertical the countable kinds happen to be exactly the
        non-auditable ones, so inferring it would agree here and quietly disagree
        for a vertical that reports a kind it also audits -- a second path giving
        the same answer, where only the reason is wrong.
        """

    def is_expected_live(self, declared_type: Optional[str]) -> bool:
        """Whether a point of this type should be producing a value at all."""

    def template_pattern(self, declared_name: str) -> object:
        """A matcher for a declared name carrying substitutions, or None.

        None means *this name is not a template, or its variables are not ones
        this domain knows* -- and an unrecognised variable must never be
        wildcarded into a match-anything pattern.
        """

    def same_point(self, old: object, new: object) -> bool:
        """Whether two captures at one address describe the same point.

        A domain with no evidence to the contrary says yes: refusing a pairing
        needs a reason, and *no reason found* is not one.
        """

    def captures_comparable(self, before: object, after: object) -> bool:
        """Whether two captures support this domain's extra per-point
        comparisons. False means those comparisons are SKIPPED, never that they
        ran and found nothing."""

    def point_changes(self, old: object, new: object, *,
                      comparable: bool = False) -> Sequence[object]:
        """What changed about one point, in terms only this domain has."""

    def capture_changes(self, before: object, after: object) -> Sequence[object]:
        """What changed about the capture as a whole."""

    def capture_findings(self, capture: object) -> Sequence[object]:
        """Findings only this domain can produce from its own capture.

        The diff pairs a declaration against a capture and reports what it
        finds. A domain may see more in its own capture than that pairing can --
        two interfaces disagreeing about one point, say -- and those findings
        belong beside the diff's own rather than in a separate report nobody
        reads. A domain with nothing extra to say returns an empty sequence.
        """

    def peer_groups(self, declaration: object) -> Sequence[Mapping[str, object]]:
        """Which declared points this domain considers redundant readings of one
        thing, as candidate groups.

        The generator pairs points it is told are peers; it does not know how a
        domain decides that. One groups by physical part and channel, another
        might group by tag prefix, and neither belongs in the generator.

        A domain with no notion of redundancy returns an empty sequence.
        """

    def report_sections(self) -> Mapping[str, object]:
        """Extra top-level report keys this domain contributes, name -> builder.

        Each builder takes the capture and returns a JSON-serialisable payload.
        A vertical with nothing to add returns an empty mapping, and the report
        simply has no such key -- rather than a key holding an empty object,
        which reads as *checked and found nothing*.
        """


_REGISTERED: Optional[Vocabulary] = None


def register(vocabulary: Vocabulary) -> None:
    """Supply the vocabulary. Called by a vertical, never by the core."""
    if not vocabulary.kinds:
        raise PluginError(
            "the vocabulary offered no kinds; a classifier with an empty range "
            "cannot answer, and registering it would make that look like a pass")
    unknown = set(vocabulary.count_keys) - set(vocabulary.kinds)
    if unknown:
        raise PluginError(
            f"the vocabulary reports kinds it cannot produce: {sorted(unknown)}. "
            "A count key with no kind behind it is a column that is always zero")
    global _REGISTERED
    _REGISTERED = vocabulary


def current() -> Vocabulary:
    """The registered vocabulary, or a refusal that says how to supply one."""
    if _REGISTERED is None:
        raise VocabularyNotRegistered(
            "no vertical has registered a vocabulary. One is supplied by an "
            "entry point in the group 'bmc_sensor_audit.plugins', by the "
            "BMC_SENSOR_AUDIT_PLUGINS environment variable, or by --plugin. "
            "Running without one would classify nothing and report cleanly")
    return _REGISTERED


def registered() -> bool:
    """Whether anything has registered. For a caller deciding what to say."""
    return _REGISTERED is not None


def reset() -> None:
    """Drop the registration. For tests that need an empty registry."""
    global _REGISTERED
    _REGISTERED = None
