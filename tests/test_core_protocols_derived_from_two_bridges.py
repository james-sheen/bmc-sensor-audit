"""The neutral protocols are satisfiable by two bridges that share no code.

A protocol derived from one implementation is that implementation's types under
a neutral name. It passes every test its own author would write, and the first
foreign shape breaks it. So this file adapts BOTH: this package's own capture and
declaration, and the shape used by an independently built bridge for a different
domain -- time-ordered samples of many nodes, and an asset register -- which
shares no code with this one.

Both adapters are EXERCISED, not merely defined. `isinstance` against a
`Protocol` proves the names exist; calling every member through an adapter proves
the protocol carries something. Each test asserts its adapter produced points, so
an adapter that quietly yields nothing cannot pass.

REWRITTEN 2026-09-07 at the measured surface. The protocols carried nine members
and the consuming modules reach twenty-three; the nine came from a scan that
filtered attribute reads by variable name. The foreign adapter below is now the
load-bearing part of this file: if a domain with seventeen of the concepts can
implement all twenty-three -- six of them by answering ABSENT -- then the
protocol is implementable by something other than its author, which is the only
property that makes it a protocol.
"""

from __future__ import annotations

import ast
from pathlib import Path

from bmc_sensor_audit.core import protocols as P
from bmc_sensor_audit.inventory.entity_manager import Declaration, DeclaredSensor
from bmc_sensor_audit.inventory.redfish import LiveSensor, Walk

CORE = Path(__file__).resolve().parents[1] / "src/bmc_sensor_audit/core"


# --------------------------------------------------------------------------
# The property the core package exists for
# --------------------------------------------------------------------------

class TestTheCoreImportsNoVertical:
    def test_no_core_module_imports_the_bmc_half(self):
        """By parse, not by grep. A comment naming a module is not an import,
        and this project has already paid for a word-level scan reading prose
        about a thing as the thing."""
        vertical = {"redfish", "redfish_schema", "sensor_types", "entity_manager",
                    "declaration_source", "mock_redfish", "cli", "report"}
        offenders = []
        for path in sorted(CORE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                    names += [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names += [a.name for a in node.names]
                for n in names:
                    tail = {p for p in n.split(".")}
                    if tail & vertical:
                        offenders.append(f"{path.name}: {n}")
        assert not offenders, (
            "the neutral core imports the vertical, which is the one thing it "
            f"exists not to do: {offenders}")

    def test_the_scan_would_notice(self):
        """The check above passes on an empty directory too. This asserts the
        population it walked was not empty."""
        assert list(CORE.rglob("*.py")), "no core modules were scanned"


# --------------------------------------------------------------------------
# Adapter 1 - this package's own shapes
# --------------------------------------------------------------------------

class _BmcPoint:
    def __init__(self, sensor: LiveSensor):
        self._s = sensor

    name = property(lambda self: self._s.name)
    path = property(lambda self: self._s.path)
    reading = property(lambda self: self._s.reading)
    is_reading = property(lambda self: self._s.is_reading)
    state = property(lambda self: self._s.state)
    thresholds = property(lambda self: self._s.thresholds)
    units = property(lambda self: self._s.units)
    is_enabled = property(lambda self: self._s.is_enabled)


class _BmcCapture:
    def __init__(self, walk: Walk):
        self._w = walk

    points = property(lambda self: [_BmcPoint(s) for s in self._w.sensors])
    captured_at = property(lambda self: self._w.captured_at)
    complete = property(lambda self: self._w.complete)
    errors = property(lambda self: self._w.errors)


class _BmcDeclared:
    def __init__(self, sensor: DeclaredSensor):
        self._d = sensor

    name = property(lambda self: self._d.name)
    type = property(lambda self: self._d.type)
    display_name = property(lambda self: self._d.display_name)
    source = property(lambda self: self._d.source)
    expects_reading = property(lambda self: self._d.expects_reading)
    disabled = property(lambda self: self._d.disabled_in_config)
    thresholds = property(lambda self: self._d.thresholds)
    is_templated = property(lambda self: self._d.is_templated)


class _BmcDeclaration:
    def __init__(self, declaration: Declaration):
        self._d = declaration

    points = property(lambda self: [_BmcDeclared(s) for s in self._d.sensors])
    sources = property(lambda self: self._d.sources)
    anomalies = property(lambda self: self._d.anomalies)
    unreadable = property(lambda self: self._d.unreadable)


# --------------------------------------------------------------------------
# Adapter 2 - the other bridge: samples over time, and an asset register
#
# Six members have no counterpart there. Each answers ABSENT rather than
# pretending: no machine-reported limits, no units, nothing switched off, no
# per-pass errors, silence on whether a tag is expected to read, and no
# self-inspection of the register. Those are not stubs -- they are the honest
# answers of a domain that does not have those concepts.
# --------------------------------------------------------------------------

class _TagPoint:
    """One node, flattened out of the time-ordered samples."""

    def __init__(self, node: str, entries: list):
        self._node, self._entries = node, entries

    @property
    def name(self) -> str:
        return self._node

    @property
    def path(self) -> str:
        return self._node                      # the node IS its address there

    @property
    def reading(self):
        usable = [e.get("v") for e in self._entries if e.get("q") == "good"]
        return usable[-1] if usable else None

    @property
    def is_reading(self) -> bool:
        return any(e.get("q") == "good" for e in self._entries)

    @property
    def state(self):
        return self._entries[-1].get("q") if self._entries else None

    thresholds = property(lambda self: {})     # ABSENT: no machine limits
    units = property(lambda self: None)        # ABSENT
    is_enabled = property(lambda self: True)   # ABSENT: nothing is switched off


class _TagCapture:
    def __init__(self, walk: dict):
        index: dict = {}
        stamps = []
        for sample in walk.get("samples") or []:
            stamps.append(sample.get("t"))
            for node, reading in (sample.get("nodes") or {}).items():
                index.setdefault(node, []).append(reading)
        self._index, self._stamps = index, [s for s in stamps if s]

    points = property(lambda self: [_TagPoint(n, e) for n, e in self._index.items()])
    captured_at = property(lambda self: self._stamps[-1] if self._stamps else None)
    complete = property(lambda self: bool(self._index))
    errors = property(lambda self: ())         # ABSENT: no per-pass error record


class _TagDeclared:
    def __init__(self, asset: dict, tag: str, spec: dict):
        self._a, self._tag, self._spec = asset, tag, spec

    name = property(lambda self: self._spec["node"])
    type = property(lambda self: self._spec.get("class"))
    display_name = property(lambda self: f"{self._a['id']}.{self._tag}")
    source = property(lambda self: self._a.get("_source") or "register")
    expects_reading = property(lambda self: None)   # ABSENT: silent, so None
    disabled = property(lambda self: bool(self._spec.get("excluded")))
    thresholds = property(lambda self: ())          # DIVERGENT: on entity types
    is_templated = property(lambda self: False)     # ABSENT: no template syntax

class _TagRegister:
    def __init__(self, register: dict):
        self._r = register

    @property
    def points(self):
        return [_TagDeclared(a, tag, spec)
                for a in self._r.get("assets") or []
                for tag, spec in (a.get("tags") or {}).items()]

    sources = property(lambda self: self._r.get("_sources") or ())
    anomalies = property(lambda self: ())      # ABSENT: no self-inspection
    unreadable = property(lambda self: self._r.get("_unreadable") or ())


# --------------------------------------------------------------------------
# Exercise both, through EVERY member the protocols declare
# --------------------------------------------------------------------------

CAPTURE_MEMBERS = ("points", "captured_at", "complete", "errors")
POINT_MEMBERS = ("name", "path", "reading", "is_reading", "state",
                 "thresholds", "units", "is_enabled")
DECLARATION_MEMBERS = ("points", "sources", "anomalies", "unreadable")
DECLARED_MEMBERS = ("name", "type", "display_name", "source",
                    "expects_reading", "disabled", "thresholds", "is_templated")


def _drive(subject, members) -> dict:
    """Read every declared member. A member that raises fails here, by name."""
    seen = {}
    for member in members:
        try:
            seen[member] = getattr(subject, member)
        except Exception as error:               # noqa: BLE001
            raise AssertionError(
                f"{type(subject).__name__}.{member} raised: "
                f"{type(error).__name__}: {error}") from error
    return seen


class TestThisBridgeSatisfiesTheProtocols:
    def test_its_capture_drives_every_member(self):
        walk = Walk(sensors=[
            LiveSensor(name="Inlet", path="/redfish/v1/a", reading=21.0, state="Enabled"),
            LiveSensor(name="Dead", path="/redfish/v1/b", reading=None, state="Enabled"),
        ])
        capture = _BmcCapture(walk)
        _drive(capture, CAPTURE_MEMBERS)
        points = list(capture.points)
        assert len(points) == 2, "the adapter yielded nothing; the drive was vacuous"
        for point in points:
            _drive(point, POINT_MEMBERS)

    def test_its_declaration_drives_every_member(self):
        decl = Declaration(sensors=[
            DeclaredSensor(name="Inlet", type="Temperature", label=None,
                           record=None, source="entity-manager"),
        ])
        source = _BmcDeclaration(decl)
        _drive(source, DECLARATION_MEMBERS)
        points = list(source.points)
        assert len(points) == 1, "the adapter yielded nothing; the drive was vacuous"
        for point in points:
            _drive(point, DECLARED_MEMBERS)


class TestTheOtherBridgeSatisfiesTheProtocols:
    WALK = {"samples": [
        {"t": "2026-09-07T00:00:00Z", "nodes": {"line1.spindle": {"v": 1490.0, "q": "good"},
                                                "line1.gap": {"v": None, "q": "bad"}}},
        {"t": "2026-09-07T00:00:05Z", "nodes": {"line1.spindle": {"v": 1495.0, "q": "good"},
                                                "line1.gap": {"v": None, "q": "bad"}}},
    ]}
    REGISTER = {"assets": [
        {"id": "line1", "type": "mill", "tags": {
            "spindle": {"node": "line1.spindle", "class": "speed"},
            "gap": {"node": "line1.gap", "class": "distance"},
            "retired": {"node": "line1.retired", "class": "speed", "excluded": True}}},
    ]}

    def test_its_capture_drives_every_member(self):
        capture = _TagCapture(self.WALK)
        _drive(capture, CAPTURE_MEMBERS)
        points = list(capture.points)
        assert len(points) == 2, "the adapter yielded nothing; the drive was vacuous"
        for point in points:
            _drive(point, POINT_MEMBERS)
        by_name = {p.name: p for p in points}
        assert by_name["line1.spindle"].reading == 1495.0
        assert by_name["line1.gap"].reading is None
        assert by_name["line1.gap"].is_reading is False

    def test_its_declaration_drives_every_member(self):
        source = _TagRegister(self.REGISTER)
        _drive(source, DECLARATION_MEMBERS)
        points = list(source.points)
        assert len(points) == 3, "the adapter yielded nothing; the drive was vacuous"
        for point in points:
            _drive(point, DECLARED_MEMBERS)
        assert {p.disabled for p in points} == {True, False}, (
            "exclusion did not survive the adapter, so the audit would judge a "
            "point the register set aside")

    def test_the_six_without_counterparts_answer_ABSENT_not_fake(self):
        """The load-bearing assertion of this file.

        A domain with seventeen of the twenty-three concepts implements all
        twenty-three by declining six. If any of these started returning a
        plausible value, the protocol would be satisfied by a fiction.
        """
        point = list(_TagCapture(self.WALK).points)[0]
        assert point.thresholds == {}, "invented machine-reported limits"
        assert point.units is None, "invented a unit"
        assert point.is_enabled is True, "a domain with no on/off must answer True"
        capture = _TagCapture(self.WALK)
        assert tuple(capture.errors) == (), "invented per-pass errors"
        declared = list(_TagRegister(self.REGISTER).points)[0]
        assert declared.expects_reading is None, "answered a question it is silent on"
        assert tuple(_TagRegister(self.REGISTER).anomalies) == (), "invented anomalies"
        assert declared.is_templated is False, "invented template syntax"

    def test_the_shape_really_is_foreign(self):
        """Guards the point of this file."""
        assert "samples" in self.WALK and "nodes" in self.WALK["samples"][0], (
            "the foreign capture is no longer time-ordered samples of many "
            "nodes, so adapting it no longer proves the protocol survives a "
            "shape it was not derived from")


class TestTheProtocolsMatchTheMeasuredSurface:
    def test_every_declared_member_is_one_the_machinery_reaches(self):
        """The member lists this file drives ARE the protocol surface. If a
        protocol grows a member, this fails until the driver reads it -- which
        is what stops the protocol becoming the type it was extracted from."""
        for proto, expected in ((P.CapturedPoint, POINT_MEMBERS),
                                (P.Capture, CAPTURE_MEMBERS),
                                (P.DeclaredPoint, DECLARED_MEMBERS),
                                (P.DeclarationSource, DECLARATION_MEMBERS)):
            declared = {n for n in vars(proto) if not n.startswith("_")}
            assert declared == set(expected), (
                f"{proto.__name__} declares {sorted(declared)}, driven surface "
                f"is {sorted(expected)}")
