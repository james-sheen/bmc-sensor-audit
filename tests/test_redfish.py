"""Tests for the walk, against a Redfish target served over real HTTP.

The mock is a real `http.server`, reached through the real `RedfishClient`, not
a stubbed client. A stub tests the walker and skips the transport, and the
transport is where a walk fails in the field -- a 500 on one chassis, a subtree
that times out. Those failures have to be distinguishable from absence, so they
have to be reachable from a test.

Stage 1 acceptance criteria 2, 3 and 4 are pinned here by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_sensor_audit.inventory.diff import compare
from bmc_sensor_audit.inventory.entity_manager import parse_config_text
from bmc_sensor_audit.inventory.redfish import (
    RedfishClient, order_walks, walk_chassis, walk_from_dict,
)
from bmc_sensor_audit.testing import MockBMC, serve


def _populated(shape: str) -> MockBMC:
    bmc = MockBMC(shape=shape)
    bmc.add("Inlet Temp", reading=22.5, units="Cel",
            upper_critical=45, upper_warning=35)
    bmc.add("CMOS Battery", reading=3.1, units="V", lower_critical=2.5)
    bmc.add("Fan 1 Tach", reading=6100, units="RPM", lower_critical=1750)
    return bmc


def _walk(bmc: MockBMC):
    with serve(bmc) as url:
        return walk_chassis(RedfishClient(url))


_walk_obj = _walk


DECLARATION = parse_config_text(json.dumps({
    "Name": "Mock Baseboard",
    "Exposes": [
        {"Name": "Inlet Temp", "Type": "TMP75", "Thresholds": [
            {"Direction": "greater than", "Name": "upper critical", "Value": 45},
            {"Direction": "greater than", "Name": "upper non critical", "Value": 35}]},
        {"Name": "CMOS Battery", "Type": "ADC", "Thresholds": [
            {"Direction": "less than", "Name": "lower critical", "Value": 2.5}]},
        {"Name": "Fan 1 Tach", "Type": "AspeedFan", "Thresholds": [
            {"Direction": "less than", "Name": "lower critical", "Value": 1750}]},
    ],
}))


# --- acceptance criterion 2: both tree shapes -------------------------------

@pytest.mark.parametrize("shape", ["sensors", "legacy"])
def test_both_tree_shapes_enumerate_the_same_sensors(shape):
    """`Thermal`/`Power` are deprecated in favour of `Sensors`, and fleets run
    both -- often at different firmware levels on the same SKU."""
    walk = _walk(_populated(shape))
    assert walk.complete
    assert sorted(s.name for s in walk) == ["CMOS Battery", "Fan 1 Tach", "Inlet Temp"]


@pytest.mark.parametrize("shape", ["sensors", "legacy"])
def test_thresholds_are_read_from_either_schema_generation(shape):
    """One nests thresholds as objects with their own Reading; the other carries
    them as flat siblings. Same facts, different shape."""
    walk = _walk(_populated(shape)).by_name()
    assert walk["Inlet Temp"].thresholds == {("upper", "critical"): 45.0,
                                             ("upper", "warning"): 35.0}
    assert walk["Fan 1 Tach"].thresholds == {("lower", "critical"): 1750.0}


@pytest.mark.parametrize("shape", ["sensors", "legacy"])
def test_a_healthy_board_is_silent_in_either_shape(shape):
    report = compare(DECLARATION, _walk(_populated(shape)))
    assert report.findings == []
    assert report.exit_code == 0


def test_a_chassis_carrying_both_trees_does_not_report_every_sensor_twice():
    """Left unmerged this produces one match and one `undeclared_present` for
    every sensor on the machine -- a report that is entirely noise."""
    walk = _walk(_populated("both"))
    assert len(walk) == 3
    assert walk.shapes_seen == {"sensors", "thermal", "power"}
    assert compare(DECLARATION, walk).findings == []


def test_a_sensor_on_only_the_deprecated_interface_is_a_finding():
    """Present on one interface, absent from another. A client reading only the
    current schema does not see this sensor at all."""
    bmc = _populated("both")
    routes = bmc.routes()
    collection = routes["/redfish/v1/Chassis/1/Sensors"]
    collection["Members"] = collection["Members"][:2]      # drop Fan 1 Tach

    class OnlyLegacyFan(MockBMC):
        def routes(self):
            return routes

    walk = _walk(OnlyLegacyFan(shape="both"))
    assert walk.divergence == [("Fan 1 Tach", "thermal")]
    kinds = {f.kind for f in compare(DECLARATION, walk).findings}
    assert "interface_divergence" in kinds


# --- acceptance criterion 3: one disabled sensor, one finding ---------------

def test_disabling_one_sensor_produces_exactly_one_finding():
    """Criterion 3, verbatim: exactly one present-but-unreadable finding and no
    false positives elsewhere."""
    bmc = _populated("sensors")
    bmc.disable("CMOS Battery")
    report = compare(DECLARATION, _walk(bmc))
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.kind == "declared_disabled"
    assert finding.sensor == "CMOS Battery"
    assert report.exit_code == 1


def test_removing_one_sensor_produces_exactly_one_finding():
    bmc = _populated("sensors")
    bmc.remove("CMOS Battery")
    report = compare(DECLARATION, _walk(bmc))
    assert [(f.kind, f.sensor) for f in report.findings] == [
        ("declared_absent", "CMOS Battery")]


# --- transport failure is not absence ---------------------------------------

def test_a_failing_subtree_is_recorded_and_absence_is_withheld():
    bmc = _populated("sensors")
    bmc.fail["/redfish/v1/Chassis/1/Sensors"] = 500
    walk = _walk(bmc)
    assert not walk.complete
    assert len(walk) == 0
    report = compare(DECLARATION, walk)
    assert report.absence_withheld is True
    assert {f.kind for f in report.findings} == {"walk_incomplete"}


def test_an_unreachable_target_reports_zero_sensors_and_an_error():
    """No server at all. The distinction that matters: this is not a machine
    with no sensors."""
    walk = walk_chassis(RedfishClient("http://127.0.0.1:1"))
    assert len(walk) == 0
    assert not walk.complete
    assert walk.errors[0][0] == "/redfish/v1/Chassis"


def test_one_failing_chassis_does_not_abandon_the_others():
    bmc = _populated("sensors")
    bmc.fail["/redfish/v1/Chassis/1"] = 503
    walk = _walk(bmc)
    assert not walk.complete
    assert walk.chassis == []


# --- acceptance criterion 4: before and after -------------------------------

def test_a_before_and_after_capture_survives_a_round_trip():
    """The firmware-upgrade gate: capture before, capture after, diff both
    against the config. The capture has to be lossless for that to work."""
    original = _walk(_populated("sensors"))
    restored = walk_from_dict(json.loads(json.dumps(original.to_dict())))
    assert [s.name for s in restored] == [s.name for s in original]
    assert [s.thresholds for s in restored] == [s.thresholds for s in original]
    assert [s.reading for s in restored] == [s.reading for s in original]
    assert [s.state for s in restored] == [s.state for s in original]
    assert compare(DECLARATION, restored).findings == []


def test_a_capture_carries_no_identifying_hardware_detail():
    """The parse is the redaction. A raw chassis walk carries serial numbers,
    asset tags and MAC addresses; committing one is a fleet inventory
    disclosure. Only the parsed sensor set is written."""
    bmc = _populated("sensors")
    routes = bmc.routes()
    # Invented, and they must live here verbatim: the test asserts these exact
    # strings never reach a capture. The hygiene check wants the same strings for
    # the opposite reason, so the markers below tell it these are not real.
    routes["/redfish/v1/Chassis/1"].update({
        "SerialNumber": "SNXXXX1234", "AssetTag": "DC-RACK-14-U22",  # hygiene: synthetic
        "PartNumber": "P/N-991", "SKU": "SKU-7"})  # hygiene: synthetic

    class WithInventory(MockBMC):
        def routes(self):
            return routes

    captured = json.dumps(_walk(WithInventory(shape="sensors")).to_dict())
    for secret in ("SNXXXX1234", "DC-RACK-14-U22", "P/N-991", "SKU-7"):
        assert secret not in captured


def test_the_firmware_upgrade_gate_end_to_end(tmp_path):
    """Capture, upgrade, capture, diff. Two sensors change; the report names
    both and the exit code fails the build."""
    before = _walk(_populated("sensors"))
    (tmp_path / "before.json").write_text(json.dumps(before.to_dict()))

    after_bmc = _populated("sensors")
    after_bmc.remove("CMOS Battery")            # vendor removed it, undocumented
    after_bmc.disable("Fan 1 Tach")             # and switched this one off
    after = _walk(after_bmc)

    assert compare(DECLARATION, before).exit_code == 0
    report = compare(DECLARATION, after)
    assert {(f.kind, f.sensor) for f in report.findings} == {
        ("declared_absent", "CMOS Battery"),
        ("declared_disabled", "Fan 1 Tach")}
    assert report.exit_code == 1


# --- committed fixtures, and what they do and do not prove ------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("fixture", ["walk_sensors_tree.json",
                                     "walk_thermal_power_tree.json"])
def test_a_recorded_walk_of_either_shape_diffs_clean(fixture):
    """Criterion 2 asks for both tree shapes proven against recorded fixtures of
    each. These are committed and stable -- no ephemeral port in any path, so a
    diff of the file across runs is empty.

    Read the `_provenance` field before treating this as full satisfaction of
    that criterion: **both fixtures are synthetic**, generated by this project's
    own mock. They prove the walker reads each shape and that the recorded
    format round-trips. They cannot prove either shape resembles a real BMC,
    because the same code wrote and read them. A capture from real hardware is
    still wanted, and criterion 2 is not honestly closed until there is one.
    """
    payload = json.loads((FIXTURES / fixture).read_text())
    assert "SYNTHETIC" in payload["_provenance"]

    walk = walk_from_dict(payload)
    assert sorted(s.name for s in walk) == ["CMOS Battery", "Fan 1 Tach", "Inlet Temp"]
    assert walk.complete
    report = compare(DECLARATION, walk)
    assert report.findings == []


def test_the_two_fixtures_describe_the_same_machine():
    """Different tree shapes, identical facts. If a schema generation ever
    changes what this tool extracts, these two stop agreeing."""
    def facts(name):
        walk = walk_from_dict(json.loads((FIXTURES / name).read_text()))
        return {s.name: (s.reading, s.units, s.state, sorted(s.thresholds.items()))
                for s in walk}

    assert facts("walk_sensors_tree.json") == facts("walk_thermal_power_tree.json")


def test_a_captured_walk_is_vendored_and_declares_what_it_is():
    """The first recorded walk here that this repository did not write.

    Everything else under `fixtures/` came out of this project's own mock, so the
    same code wrote and read it. This one came from `bmcweb` running under QEMU
    on an upstream OpenBMC image, which is the only thing in the repository that
    can contradict the walker.

    The provenance string is asserted field by field because a capture without
    it is unreproducible: an image that cannot be identified is a fixture nobody
    can regenerate, and the first attempt at the upstream config directory was
    unpinned and had drifted within a day.
    """
    payload = json.loads((FIXTURES / "walk_qemu_bletchley.json").read_text())
    provenance = payload["_provenance"]
    assert "CAPTURED" in provenance
    assert "SYNTHETIC" not in provenance
    for fact in ("bletchley-bmc", "10.2.1", "latest-master build 1714", "bmcweb",
                 "obmc-phosphor-image-bletchley-20260815025045.static.mtd",
                 "eeprom@56"):
        assert fact in provenance, f"the provenance no longer records {fact!r}"

    walk = walk_from_dict(payload)
    assert walk.complete
    assert [s.name for s in walk] == [s["name"] for s in payload["sensors"]], \
        "the recorded format no longer round-trips a real capture"
    assert payload["shapes_seen"] == ["sensors"], \
        "the capture no longer carries the modern tree shape it was taken for"
    assert all(s.units for s in walk), "a real capture with a unitless sensor"


def test_the_captured_walk_is_not_the_mock_wearing_a_new_name():
    """Guards the one property the fixture exists for. If its sensors ever match
    what the mock serves, someone has regenerated it locally and the criterion-2
    evidence has quietly become circular again."""
    captured = walk_from_dict(
        json.loads((FIXTURES / "walk_qemu_bletchley.json").read_text()))
    synthetic = walk_from_dict(
        json.loads((FIXTURES / "walk_sensors_tree.json").read_text()))
    assert not ({s.name for s in captured} & {s.name for s in synthetic})


def test_every_vendored_walk_declares_its_provenance():
    """Derived from the directory, never from a list written here. A fixture
    added without provenance is exactly the drift this checks for, and a
    transcribed filename list would not see it.

    It globbed `walk_*.json` until 2026-08-18, which is a transcription wearing a
    glob's clothes: the naming convention was the list. Two fixtures added that day
    -- a walk series and a set of recorded Redfish documents -- were named for what
    they are rather than for the pattern, carried provenance, and were checked by
    nothing. Now every fixture in the directory answers, whatever it is called.
    """
    walks = sorted(FIXTURES.glob("*.json"))
    assert len(walks) >= 5, "the fixture set shrank"
    kinds = {}
    for path in walks:
        payload = json.loads(path.read_text())
        assert "_provenance" in payload, f"{path.name} carries no provenance"
        kinds[path.name] = ("CAPTURED" if "CAPTURED" in payload["_provenance"]
                            else "SYNTHETIC" if "SYNTHETIC" in payload["_provenance"]
                            else "UNDECLARED")
    assert "UNDECLARED" not in kinds.values(), kinds
    assert "CAPTURED" in kinds.values(), \
        "no captured walk remains; criterion 2 is back to fixtures this project wrote"
    assert "SYNTHETIC" in kinds.values(), \
        "the synthetic pair covers the deprecated tree shape and must not be dropped"


class TestTheDeprecatedShapeAgainstRealFirmware:
    """The other half of criterion 2, and the only real evidence for it.

    Every other test of the deprecated `Thermal`/`Power` tree in this repository
    feeds the walker a document this project wrote, so it can only show that the
    reader reads what we thought firmware sends. `redfish_witherspoon_2_9_0.json`
    holds the bytes an actual `bmcweb` sent: OpenBMC release 2.9.0, published 2021,
    booted under QEMU. That firmware predates `ThermalSubsystem` entirely, so it
    serves a populated `Thermal` tree while its modern `Sensors` collection is
    empty -- the exact mirror of `walk_qemu_bletchley.json`, which is current
    firmware serving only the modern shape.

    The documents are replayed over the same real `http.server` the mock uses and
    read through the real `RedfishClient`, because a stubbed client would skip the
    transport this file exists to keep in the loop. What changes is only the source
    of the bytes: recorded rather than generated.
    """

    DOCUMENTS = FIXTURES / "redfish_witherspoon_2_9_0.json"

    @classmethod
    def _fixture(cls):
        return json.loads(cls.DOCUMENTS.read_text())

    @staticmethod
    def _walk(documents):
        class _Recorded:
            fail: dict = {}

            def routes(self):
                return dict(documents)

        with serve(_Recorded()) as url:
            return walk_chassis(RedfishClient(url))

    def test_the_recorded_documents_replay_into_real_sensors(self):
        walk = self._walk(self._fixture()["documents"])
        assert not walk.errors
        assert sorted(walk.shapes_seen) == ["power", "sensors", "thermal"]
        by_shape = [s.source_shape for s in walk.sensors]
        assert len(walk.sensors) == 6, [s.name for s in walk.sensors]
        assert set(by_shape) == {"thermal"}

    def test_the_legacy_threshold_fields_are_read_from_real_bytes(self):
        """`LowerThresholdCritical` and friends, as 2021 firmware spells them. The
        modern capture cannot exercise this: it carries the nested `Thresholds`
        object instead."""
        walk = self._walk(self._fixture()["documents"])
        ambient = next(s for s in walk.sensors if s.name == "ambient")
        assert ambient.reading == 22.738
        assert ambient.thresholds[("upper", "critical")] == 35.0
        assert ambient.thresholds[("upper", "warning")] == 25.0

    def test_this_firmware_serves_nothing_on_the_modern_collection(self):
        """The property that makes it worth vendoring. If the modern collection
        ever answers here, the fixture has stopped being the deprecated-shape
        counterpart and the coverage it is cited for is gone."""
        docs = self._fixture()["documents"]
        assert docs["/redfish/v1/Chassis/chassis/Sensors"]["Members@odata.count"] == 0

    def test_a_power_control_carrying_no_reading_is_not_counted_as_a_sensor(self):
        """Exercised against the real control object, not an invented one.

        This firmware's `Power` document has empty `Voltages` and `PowerSupplies`
        and a single `PowerControl` entry that is a power-limit knob with no
        measurement in it. Emitting that as a reading-less sensor would invent one
        from a control, and every downstream count would inherit it.
        """
        docs = self._fixture()["documents"]
        control = docs["/redfish/v1/Chassis/chassis/Power"]["PowerControl"]
        assert len(control) == 1 and "PowerConsumedWatts" not in control[0]

        walk = self._walk(docs)
        assert not [s for s in walk.sensors if s.source_shape == "power"]

    def test_a_power_control_carrying_a_reading_is_counted(self):
        """SYNTHETIC, and labelled because it has to be.

        `PowerConsumedWatts` is where the deprecated schema puts chassis draw, and
        the walker skipped `PowerControl` entirely until 2026-08-18 while the object
        parser already knew the key -- a branch nothing could reach. Real firmware
        found the gap by publishing the array, but neither 2.9.0 machine available
        publishes a value in it, so the repaired path can only be shown with a value
        added here. That is weaker evidence than the rest of this class and is worth
        replacing the moment a capture carries one.
        """
        docs = json.loads(json.dumps(self._fixture()["documents"]))
        docs["/redfish/v1/Chassis/chassis/Power"]["PowerControl"][0].update(
            {"Name": "Chassis Power", "PowerConsumedWatts": 137.5})

        walk = self._walk(docs)
        drawn = [s for s in walk.sensors if s.source_shape == "power"]
        assert [(s.name, s.reading) for s in drawn] == [("Chassis Power", 137.5)]

    def test_the_fixture_records_what_it_does_not_prove(self):
        provenance = self._fixture()["_provenance"]
        assert "CAPTURED" in provenance
        for fact in ("2.9.0", "witherspoon-bmc", "10.2.1",
                     "redfish-allow-deprecated-power-thermal"):
            assert fact in provenance, f"the provenance no longer records {fact!r}"
        assert "what this is not" in provenance.lower()


class TestWalkLatencyIsRecorded:
    """One field, taken where every fetch already passes through.

    A BMC whose Redfish stack is degrading answers more slowly long before it
    answers wrongly, and nothing else in this tool can see that. The walker already
    touches every endpoint, so the measurement costs a clock read.
    """

    def test_every_fetch_is_timed(self):
        walk = _walk(_populated("sensors"))
        assert walk.latencies, "no fetch was timed"
        assert all(t >= 0 for _, t in walk.latencies), "a negative interval"
        assert all(isinstance(path, str) and path for path, _ in walk.latencies)
        assert any("Chassis" in path for path, _ in walk.latencies)

    def test_a_reused_client_does_not_carry_the_previous_walk(self):
        """The measurement bug that reads as a BMC getting slower while nothing
        changed: a second walk inheriting the first one\'s timings would double the
        fetch count and drag the tail."""
        with serve(_populated("sensors")) as url:
            client = RedfishClient(url)
            first = walk_chassis(client)
            second = walk_chassis(client)
        assert len(second.latencies) == len(first.latencies)

    def test_latency_survives_a_round_trip_through_a_capture(self):
        walk = _walk(_populated("sensors"))
        rehydrated = walk_from_dict(walk.to_dict())
        assert len(rehydrated.latencies) == len(walk.latencies)
        assert rehydrated.latencies[0][0] == walk.latencies[0][0]

    def test_a_capture_taken_before_this_existed_reads_as_ABSENT_not_zero(self):
        """Absent and zero are different facts. A walk with no `latencies` key is
        one nobody measured; reading it as a list of zeroes would report the
        fastest BMC ever built -- and every vendored fixture predates this field."""
        payload = _walk(_populated("sensors")).to_dict()
        del payload["latencies"]
        assert walk_from_dict(payload).latencies == []

    def test_the_vendored_capture_predates_this_and_is_read_without_complaint(self):
        walk = walk_from_dict(json.loads(
            (Path(__file__).resolve().parents[1]
             / "tests" / "fixtures" / "walk_qemu_bletchley.json").read_text()))
        assert walk.latencies == []
        assert len(walk) == 28, "the capture itself should be unaffected"


class TestWalksAreOrderedByWhenTheyWereTaken:
    """The last walk supplies every current reading, so the order is not a
    presentation detail.

    Measured before this existed: two hundred captures passed as a shell glob
    arrive in LEXICAL order -- `walk10` before `walk9` -- and the run reported a
    reading of 108 where the newest capture said 209. It announced walk 99 as the
    present state of the machine, and every bound verdict was judged against it.
    """

    def _walk(self, stamp):
        walk = _walk_obj(_populated("sensors"))
        walk.captured_at = stamp
        return walk

    def test_a_capture_records_when_it_was_taken(self):
        walk = _walk_obj(_populated("sensors"))
        assert walk.captured_at, "a live walk carries no capture time"
        assert walk.captured_at.endswith("+00:00"), "the stamp is not UTC"

    def test_the_stamp_survives_a_round_trip(self):
        walk = _walk_obj(_populated("sensors"))
        assert walk_from_dict(walk.to_dict()).captured_at == walk.captured_at

    def test_reserialising_an_old_capture_does_not_restamp_it(self):
        """Serialising is not observing. A walk rehydrated from a year-old capture
        and written back out must not claim to have been taken today, which is the
        one thing a timestamp exists to prevent."""
        original = {"format": "bmc-sensor-audit/walk/1", "chassis": [],
                    "shapes_seen": [], "errors": [], "sensors": [],
                    "captured_at": "2020-01-01T00:00:00+00:00"}
        assert walk_from_dict(original).to_dict()["captured_at"] == \
            "2020-01-01T00:00:00+00:00"

    def test_lexical_order_is_corrected_to_chronological(self):
        walks = [self._walk("2026-01-01T00:0%d:00+00:00" % i) for i in (1, 9, 2)]
        ordered, note = order_walks(walks)
        assert [w.captured_at for w in ordered] == sorted(
            w.captured_at for w in walks)
        assert note and "reordered" in note

    def test_an_order_that_was_already_right_says_nothing(self):
        """A note on every run is a note nobody reads."""
        walks = [self._walk("2026-01-01T00:0%d:00+00:00" % i) for i in (1, 2, 3)]
        ordered, note = order_walks(walks)
        assert note is None
        assert ordered == walks

    def test_walks_with_no_stamp_keep_their_order_and_say_so(self):
        """Old captures predate the field, so this is a warning rather than a
        refusal -- but silence would leave the caller believing the tool checked."""
        walks = [self._walk(None), self._walk(None)]
        ordered, note = order_walks(walks)
        assert ordered == walks
        assert note and "no capture time" in note

    def test_a_partial_ordering_is_refused_rather_than_applied(self):
        """A subset is never sorted. Putting the stamped walks in order and leaving
        the rest where they fell produces a confident sequence that is wrong in an
        unpredictable place."""
        walks = [self._walk("2026-01-01T00:05:00+00:00"), self._walk(None),
                 self._walk("2026-01-01T00:01:00+00:00")]
        ordered, note = order_walks(walks)
        assert ordered == walks, "a partial ordering was applied"
        assert note and "not an ordering" in note

    def test_a_single_walk_is_never_commented_on(self):
        """One walk has no order to get wrong, and a warning there would be noise
        on the most common invocation there is."""
        walks = [self._walk(None)]
        ordered, note = order_walks(walks)
        assert ordered == walks and note is None
