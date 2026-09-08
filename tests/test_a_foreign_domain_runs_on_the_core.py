"""A domain this package was not written for, running on its neutral core.

No BMC anything: a factory-line vocabulary, factory-line data in its own shape
-- time-ordered samples of many nodes, and an asset register -- adapted onto the
protocols, driven through the same `compare()` the presence audit is written in.

This is the measurement the neutral-core work exists to produce. If it stops
passing, the core has stopped being shared and is only this domain's code with
an indirection.

The stand-ins below implement the published protocols and NOTHING ELSE -- no
`__iter__`, no `__len__`, no convenience the concrete BMC types happen to have.
That is the point of them. A fixture that adds a member to get past a call site
proves the core can serve a domain that already knows what the document does not
say, which is not the claim.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bmc_sensor_audit.core import protocols as P
from bmc_sensor_audit.core import vocabulary as V
from bmc_sensor_audit.inventory.diff import compare
from bmc_sensor_audit.inventory.regression import compare_walks

KINDS = ("point", "not_a_point", "unrecognised")


class _P:                                    # a captured node
    def __init__(s, node, entries): s._n, s._e = node, entries
    name = property(lambda s: s._n)
    path = property(lambda s: s._n)
    @property
    def reading(s):
        good = [e.get("v") for e in s._e if e.get("q") == "good"]
        return good[-1] if good else None
    is_reading = property(lambda s: any(e.get("q") == "good" for e in s._e))
    state = property(lambda s: s._e[-1].get("q") if s._e else None)
    thresholds = property(lambda s: {})
    units = property(lambda s: None)
    is_enabled = property(lambda s: True)


class _C:                                    # the capture
    def __init__(s, walk):
        idx, stamps = {}, []
        for sample in walk.get("samples") or []:
            stamps.append(sample.get("t"))
            for node, r in (sample.get("nodes") or {}).items():
                idx.setdefault(node, []).append(r)
        s._i, s._t = idx, [x for x in stamps if x]
    points = property(lambda s: [_P(n, e) for n, e in s._i.items()])
    captured_at = property(lambda s: s._t[-1] if s._t else None)
    complete = property(lambda s: bool(s._i))
    errors = property(lambda s: ())
    # No `__iter__`, no `__len__`. This class is exactly `protocols.Capture` and
    # nothing else, because the protocol is the whole contract an outside domain
    # is given. Adding a member here to make a call site work would move the
    # requirement somewhere no reader of the protocol can find it.


class _D:                                    # a declared tag
    def __init__(s, asset, tag, spec): s._a, s._t, s._s = asset, tag, spec
    name = property(lambda s: s._s["node"])
    type = property(lambda s: s._s.get("class"))
    display_name = property(lambda s: f"{s._a['id']}.{s._t}")
    source = property(lambda s: "register.yaml")
    expects_reading = property(lambda s: None)
    disabled = property(lambda s: bool(s._s.get("excluded")))
    thresholds = property(lambda s: ())
    is_templated = property(lambda s: False)


class _DS:                                   # the declaration
    def __init__(s, reg): s._r = reg
    @property
    def points(s):
        return [_D(a, t, sp) for a in s._r.get("assets") or []
                for t, sp in (a.get("tags") or {}).items()]
    sources = property(lambda s: ("register.yaml",))
    anomalies = property(lambda s: ())
    unreadable = property(lambda s: ())
    # Exactly `protocols.DeclarationSource`, for the reason above.


class FactoryLineVocabulary:
    kinds = property(lambda s: KINDS)
    count_keys = property(lambda s: {"not_a_point": "not_a_point"})
    def classify(s, t): return "point" if t in ("speed", "distance") else "unrecognised"
    def is_auditable(s, kind): return kind == "point"
    def is_expected_live(s, t): return s.classify(t) == "point"
    def template_pattern(s, name): return None
    def same_point(s, old, new): return True
    def point_changes(s, old, new, *, comparable=False): return []
    def capture_changes(s, before, after): return []
    def captures_comparable(s, before, after): return False
    def capture_findings(s, capture): return []
    def peer_groups(s, declaration): return []
    def report_sections(s): return {}


WALK = {"samples": [
    {"t": "2026-09-07T00:00:00Z", "nodes": {
        "line1.spindle": {"v": 1490.0, "q": "good"},
        "line1.gap":     {"v": None,   "q": "bad"},
        "line1.rogue":   {"v": 3.0,    "q": "good"}}},
    {"t": "2026-09-07T00:00:05Z", "nodes": {
        "line1.spindle": {"v": 1495.0, "q": "good"},
        "line1.gap":     {"v": None,   "q": "bad"},
        "line1.rogue":   {"v": 3.1,    "q": "good"}}}]}
REGISTER = {"assets": [{"id": "line1", "type": "mill", "tags": {
    "spindle": {"node": "line1.spindle", "class": "speed"},
    "gap":     {"node": "line1.gap",     "class": "distance"},
    "absent":  {"node": "line1.absent",  "class": "speed"},
    "retired": {"node": "line1.retired", "class": "speed", "excluded": True}}}]}

V.reset(); V.register(FactoryLineVocabulary())


@pytest.fixture
def factory_line_registered():
    """The vocabulary alone, for tests that drive an entry point themselves."""
    previous = V._REGISTERED
    V.reset()
    V.register(FactoryLineVocabulary())
    try:
        yield
    finally:
        V._REGISTERED = previous


@pytest.fixture
def foreign_report():
    previous = V._REGISTERED
    V.reset()
    V.register(FactoryLineVocabulary())
    try:
        yield compare(_DS(REGISTER), _C(WALK))
    finally:
        V._REGISTERED = previous


class TestTheCoreServesADomainItWasNotWrittenFor:
    def test_it_produces_a_three_valued_presence_diff(self, foreign_report):
        kinds = {f.kind for f in foreign_report.findings}
        assert kinds == {"declared_unreadable", "declared_absent",
                         "undeclared_present"}, (
            f"the core did not produce a presence diff for this domain: {kinds}")

    def test_each_of_the_three_lands_on_the_right_tag(self, foreign_report):
        by_kind = {f.kind: f.sensor for f in foreign_report.findings}
        assert by_kind["declared_unreadable"] == "line1.gap"
        assert by_kind["declared_absent"] == "line1.absent"
        assert by_kind["undeclared_present"] == "line1.rogue"

    def test_the_excluded_tag_is_not_judged(self, foreign_report):
        """`line1.retired` is excluded by the register. Four tags are declared;
        three are audited. A core that counted four would be judging a point the
        domain set aside."""
        assert foreign_report.counts()["declared"] == 3

    def test_the_adapter_offers_ONLY_protocol_members(self):
        """The load-bearing assertion.

        If this adapter carried a member the protocol does not declare, the run
        above would prove the core works against THIS package's names rather
        than against the protocol. It offered `sensors` and
        `disabled_in_config` once, and the core read them; those reads are gone
        and so are the shims.
        """
        import ast
        import inspect as _inspect
        declared = set()
        for cls in (_P, _C, _D, _DS):
            declared |= {n for n in vars(cls) if not n.startswith("_")}
        allowed = set()
        for node in ast.parse(_inspect.getsource(P)).body:
            if isinstance(node, ast.ClassDef):
                allowed |= {x.name for x in node.body
                            if isinstance(x, ast.FunctionDef)}
        extra = declared - allowed
        assert not extra, (
            f"the foreign adapter offers non-protocol members {sorted(extra)}, "
            f"so this file proves less than it claims")


class TestTheProtocolIsTheWholeContract:
    """Every neutral entry point, driven by stand-ins that are ONLY the protocol.

    `compare()` has the foreign-domain suite above. `compare_walks()` had
    nothing: it is in the same neutral band, takes the same `Capture`, and no
    test drove it with anything but the concrete BMC walk -- which carries
    members the protocol does not declare, so it could not have noticed needing
    them. A band is claimed neutral per module; it has to be checked per module.
    """

    def test_compare_reads_points_on_both_arguments(self, factory_line_registered):
        report = compare(_DS(REGISTER), _C(WALK))
        assert report.counts()["declared"] > 0, (
            "a declaration implementing exactly the protocol produced nothing")

    def test_compare_walks_reads_points_on_both_captures(self, factory_line_registered):
        report = compare_walks(_C(WALK), _C(WALK))
        assert report.before_count == report.after_count == len(_C(WALK).points)
        assert report.before_count > 0, "the fixture cannot refute anything empty"

    @pytest.mark.parametrize("member", ["__iter__", "__len__"])
    def test_the_stand_ins_really_lack_the_members_the_concrete_types_have(self, member):
        """The assertion the two tests above are only meaningful because of.

        If a later edit adds either member back to these stand-ins for
        convenience, both tests keep passing while testing nothing, and the
        requirement moves back somewhere no reader of the protocol can find it.
        """
        for cls in (_C, _DS):
            assert not hasattr(cls, member), (
                f"{cls.__name__} defines {member}, which `core/protocols.py` does "
                f"not declare -- so these tests no longer measure the protocol")

