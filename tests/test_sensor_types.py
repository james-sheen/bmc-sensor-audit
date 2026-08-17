"""The `Type` filter: three buckets, and the third is the one that matters.

`Type` was read and never used, so every `Exposes` entry was expected to appear in a
Redfish `Sensors` collection. PID loops, stepwise fan curves, EEPROMs, firmware blobs,
muxes and GPIO presence detectors cannot, so all of them became `declared_absent`
regressions and the gate went red on healthy hardware — 2,121 of 8,684 upstream
declarations, about 24 %.

The regression test that matters is `TestAHealthyBoardIsClean`: a board whose one real
sensor is present and reading must exit 0. That case exited 1 before this landed, and it
is the whole defect in four lines.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory import sensor_types as st  # noqa: E402
from bmc_sensor_audit.inventory.diff import compare  # noqa: E402
from bmc_sensor_audit.inventory.entity_manager import load_declaration  # noqa: E402
from bmc_sensor_audit.inventory.redfish import walk_from_dict  # noqa: E402


def _walk(names):
    return walk_from_dict({
        "format": "bmc-sensor-audit/walk/1",
        "chassis": ["/redfish/v1/Chassis/1"], "shapes_seen": ["sensors"], "errors": [],
        "sensors": [{"name": n, "path": f"/redfish/v1/Chassis/1/Sensors/s{i}",
                     "reading": 20.0 + i, "state": "Enabled", "health": "OK",
                     "thresholds": {}} for i, n in enumerate(names)]})


def _config(tmp_path, entries):
    path = tmp_path / "board.json"
    path.write_text(json.dumps({"Name": "Board", "Exposes": entries}))
    return load_declaration([str(path)])


class TestTheSetsThemselves:
    def test_the_two_sets_do_not_intersect(self):
        """Not decoration. This check caught `XeonCPU` and `ModifiedMedian`, both put
        in the non-sensor list by hand while the corpus showed them declaring
        thresholds — a CPU package temperature and a virtual-sensor aggregation, both
        obviously sensors once the evidence was consulted."""
        overlap = st.KNOWN_SENSOR & st._NOT_A_SENSOR_EXPLICIT
        assert overlap == frozenset(), f"a Type is classified both ways: {sorted(overlap)}"

    def test_evidence_beats_a_suffix_guess(self):
        """`KNOWN_SENSOR` is consulted first, so a real sensor whose name happens to
        end in a family suffix stays a sensor. Without this ordering the suffix rules
        would quietly outrank the measured evidence."""
        for name in st.KNOWN_SENSOR:
            assert st.classify(name) == st.SENSOR, \
                f"{name} is evidenced as a sensor but classifies as {st.classify(name)}"

    def test_the_kind_vocabulary_is_closed_and_complete(self):
        assert set(st.KINDS) == {st.SENSOR, st.NOT_A_SENSOR, st.UNRECOGNISED}


class TestClassification:
    @pytest.mark.parametrize("sensor_type", ["TMP75", "pmbus", "ADC", "XeonCPU"])
    def test_known_sensors_are_sensors(self, sensor_type):
        assert st.classify(sensor_type) == st.SENSOR

    @pytest.mark.parametrize("sensor_type", [
        "Pid", "Pid.Zone", "Stepwise", "EEPROM", "GPIODeviceDetect",
        "GPIOLeakDetector", "IntelFanConnector",
    ])
    def test_the_named_non_sensors_are_excluded(self, sensor_type):
        assert st.classify(sensor_type) == st.NOT_A_SENSOR

    @pytest.mark.parametrize("sensor_type", [
        "LatticeLCMXO3D_9400Firmware", "PCA9545Mux", "USBPort",
        "Danfoss003Z8540Valve", "DeltaECD17020037PowerSupplyUnit",
    ])
    def test_suffix_families_are_excluded_without_being_listed(self, sensor_type):
        """A vendor adding another firmware blob should not require an edit here."""
        assert st.classify(sensor_type) == st.NOT_A_SENSOR

    @pytest.mark.parametrize("sensor_type", ["HPEFan", "SomethingNobodyHasSeen", "", None])
    def test_anything_else_is_unrecognised_not_guessed(self, sensor_type):
        """The third bucket. A closed split would force an unseen Type into whichever
        default it happened to have, confidently and silently."""
        assert st.classify(sensor_type) == st.UNRECOGNISED

    def test_only_a_known_sensor_is_expected_live(self):
        assert st.is_expected_live("TMP75")
        assert not st.is_expected_live("Pid")
        assert not st.is_expected_live("HPEFan")


class TestAHealthyBoardIsClean:
    """The defect, and the reason all of the above exists."""

    ENTRIES = [
        {"Name": "Inlet Temp", "Type": "TMP75"},
        {"Name": "Fan Zone 1", "Type": "Pid"},
        {"Name": "Fan Curve", "Type": "Stepwise"},
        {"Name": "FRU EEPROM", "Type": "EEPROM"},
        {"Name": "Cable Present", "Type": "GPIODeviceDetect"},
    ]

    def test_a_board_whose_only_sensor_reports_exits_zero(self, tmp_path):
        report = compare(_config(tmp_path, self.ENTRIES), _walk(["Inlet Temp"]))
        assert report.exit_code == 0, \
            f"healthy board still reports regressions: {[str(f) for f in report.regressions]}"

    def test_the_excluded_entries_are_counted_not_dropped(self, tmp_path):
        """An exclusion nobody can see is indistinguishable from a checker that forgot
        to look. If these counts vanish, the filter has become invisible."""
        report = compare(_config(tmp_path, self.ENTRIES), _walk(["Inlet Temp"]))
        counts = report.counts()
        assert counts["not_a_sensor"] == 4
        assert counts["declared"] == 1, "non-sensors are still inflating the denominator"

    def test_an_unrecognised_type_is_reported_and_not_a_regression(self, tmp_path):
        entries = self.ENTRIES + [{"Name": "Mystery", "Type": "HPEFan"}]
        report = compare(_config(tmp_path, entries), _walk(["Inlet Temp"]))
        assert report.counts()["unrecognised_type"] == 1
        assert report.exit_code == 0, "an unclassifiable type must not fail the gate"
        assert any(s.name == "Mystery"
                   for s in report.not_sensor_kinds[st.UNRECOGNISED])

    def test_a_genuinely_missing_sensor_is_still_a_regression(self, tmp_path):
        """The paired negative, and the one that matters most. A filter that silenced
        everything would pass every test above and destroy the product."""
        entries = self.ENTRIES + [{"Name": "Outlet Temp", "Type": "TMP75"}]
        report = compare(_config(tmp_path, entries), _walk(["Inlet Temp"]))
        assert report.exit_code == 1, "a real sensor that vanished no longer fails"
        assert any(f.sensor == "Outlet Temp" for f in report.regressions)
