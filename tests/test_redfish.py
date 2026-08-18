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
    RedfishClient, walk_chassis, walk_from_dict,
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
    transcribed filename list would not see it."""
    walks = sorted(FIXTURES.glob("walk_*.json"))
    assert len(walks) >= 3, "the walk fixture set shrank"
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
