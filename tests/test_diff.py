"""Tests for the comparison itself.

The three-way classification is the product, and each of its three outcomes is
a different hardware condition with a different response. Most of these tests
exist because the two-way version -- present or absent -- is easier to write and
loses the case the tool was built for.
"""

from __future__ import annotations

import json

from bmc_sensor_audit.inventory.diff import compare
from bmc_sensor_audit.inventory.entity_manager import parse_config_text
from bmc_sensor_audit.inventory.redfish import Walk, read_sensor_object

CONFIG = {
    "Name": "Test Baseboard",
    "Exposes": [
        {"Name": "Inlet Temp", "Type": "TMP75", "Thresholds": [
            {"Direction": "greater than", "Name": "upper critical", "Value": 45},
            {"Direction": "greater than", "Name": "upper non critical", "Value": 35}]},
        {"Name": "CMOS Battery", "Type": "ADC"},
        {"Name": "Fan 1 Tach", "Type": "AspeedFan"},
    ],
}


def _walk(*objects, errors=()):
    walk = Walk()
    for obj in objects:
        walk.sensors.append(read_sensor_object(obj, f"/redfish/v1/S/{obj['Name']}"))
    walk.errors = list(errors)
    return walk


def _declaration(config=None):
    return parse_config_text(json.dumps(config or CONFIG))


def _kinds(report):
    return {f.kind for f in report.findings}


def test_a_present_and_reading_sensor_produces_nothing():
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "Status": {"State": "Enabled"},
         "UpperThresholdCritical": 45, "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": 3.1, "Status": {"State": "Enabled"}},
        {"Name": "Fan 1 Tach", "Reading": 6100, "Status": {"State": "Enabled"}})
    report = compare(_declaration(), walk)
    assert report.findings == []
    assert report.exit_code == 0
    assert report.counts()["reading"] == 3


def test_the_dell_case_a_sensor_that_vanished():
    """The CMOS battery sensor that disappeared after a firmware upgrade. Not
    failed -- absent. Nothing in the reading stream can see this."""
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdCritical": 45,
         "UpperThresholdNonCritical": 35},
        {"Name": "Fan 1 Tach", "Reading": 6100})
    report = compare(_declaration(), walk)
    absent = [f for f in report.findings if f.kind == "declared_absent"]
    assert [f.sensor for f in absent] == ["CMOS Battery"]
    assert report.exit_code == 1


def test_the_dgx_case_present_but_switched_off():
    """A disabled sensor does not appear in the BMC web UI at all. Present and
    absent are both the wrong answer for it."""
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdCritical": 45,
         "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Status": {"State": "Disabled"}},
        {"Name": "Fan 1 Tach", "Reading": 6100})
    report = compare(_declaration(), walk)
    assert _kinds(report) == {"declared_disabled"}
    finding = report.findings[0]
    assert finding.sensor == "CMOS Battery"
    assert "Disabled" in finding.detail


def test_present_and_enabled_but_carrying_no_reading():
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdCritical": 45,
         "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": None, "Status": {"State": "Enabled"}},
        {"Name": "Fan 1 Tach", "Reading": 6100})
    report = compare(_declaration(), walk)
    assert _kinds(report) == {"declared_unreadable"}


def test_a_reading_of_zero_is_not_a_missing_reading():
    """Collapsing null into zero loses the finding and invents a healthy value."""
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdCritical": 45,
         "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": 0},
        {"Name": "Fan 1 Tach", "Reading": 0})
    report = compare(_declaration(), walk)
    assert report.findings == []


def test_the_reverse_direction_is_reported():
    """Sensors the machine reports that the config never declared. This is how
    an unrecorded hardware variant shows up."""
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdCritical": 45,
         "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": 3.1},
        {"Name": "Fan 1 Tach", "Reading": 6100},
        {"Name": "Mystery Rail", "Reading": 12.0})
    report = compare(_declaration(), walk)
    undeclared = [f for f in report.findings if f.kind == "undeclared_present"]
    assert [f.sensor for f in undeclared] == ["Mystery Rail"]
    # Informational, not a regression: an extra sensor is not a coverage failure.
    assert report.exit_code == 0


def test_threshold_drift_is_its_own_finding():
    """A firmware update that keeps the sensor and widens its limits is
    invisible to presence checking and to ordinary alerting alike, because
    nothing ever breaches a threshold that moved."""
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5,
         "UpperThresholdCritical": 60, "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": 3.1},
        {"Name": "Fan 1 Tach", "Reading": 6100})
    report = compare(_declaration(), walk)
    drift = [f for f in report.findings if f.kind == "threshold_drift"]
    assert len(drift) == 1
    assert "declared 45" in drift[0].detail and "live 60" in drift[0].detail


def test_a_declared_threshold_absent_on_the_machine():
    walk = _walk(
        {"Name": "Inlet Temp", "Reading": 22.5, "UpperThresholdNonCritical": 35},
        {"Name": "CMOS Battery", "Reading": 3.1},
        {"Name": "Fan 1 Tach", "Reading": 6100})
    report = compare(_declaration(), walk)
    assert _kinds(report) == {"threshold_missing"}


def test_an_incomplete_walk_withholds_absence_findings():
    """A transport failure that renders as '47 sensors missing' is worse than no
    report, because someone will act on it."""
    walk = _walk({"Name": "Inlet Temp", "Reading": 22.5,
                  "UpperThresholdCritical": 45, "UpperThresholdNonCritical": 35},
                 errors=[("/redfish/v1/Chassis/2", "timed out")])
    report = compare(_declaration(), walk)
    assert report.absence_withheld is True
    assert "declared_absent" not in _kinds(report)
    assert "walk_incomplete" in _kinds(report)
    assert report.counts()["declared_absent"] == 0


def test_the_same_walk_completed_does_report_absence():
    """The paired positive: identical data, no transport error, absence reported.
    Without this, the test above passes just as well if absence never works."""
    walk = _walk({"Name": "Inlet Temp", "Reading": 22.5,
                  "UpperThresholdCritical": 45, "UpperThresholdNonCritical": 35})
    report = compare(_declaration(), walk)
    assert report.absence_withheld is False
    assert "declared_absent" in _kinds(report)
    assert report.counts()["declared_absent"] == 2


def test_a_templated_name_matches_its_substituted_form_and_says_so():
    config = {"Exposes": [{"Name": "$bus_ADC0", "Type": "ADC"}]}
    walk = _walk({"Name": "13_ADC0", "Reading": 1.0})
    report = compare(_declaration(config), walk)
    assert len(report.matches) == 1
    assert report.matches[0].how == "template"
    # The pairing is surfaced, never silent.
    assert "matched_inexactly" in _kinds(report)


def test_a_template_cannot_match_an_unrelated_sensor():
    config = {"Exposes": [{"Name": "$bus_ADC0", "Type": "ADC"}]}
    walk = _walk({"Name": "P12V_AUX", "Reading": 12.0})
    report = compare(_declaration(config), walk)
    assert report.matches == []
    assert "declared_absent" in _kinds(report)


def test_an_exact_match_is_never_stolen_by_a_template():
    """Ordering matters: templates are tried last, so a pattern cannot claim a
    sensor that a literal name would have paired with."""
    config = {"Exposes": [{"Name": "$bus_ADC0", "Type": "ADC"},
                          {"Name": "13_ADC0", "Type": "ADC"}]}
    walk = _walk({"Name": "13_ADC0", "Reading": 1.0})
    report = compare(_declaration(config), walk)
    assert len(report.matches) == 1
    assert report.matches[0].declared.name == "13_ADC0"
    assert report.matches[0].how == "exact"


def test_config_disabled_sensors_are_not_expected_by_default():
    """94 upstream entries carry Status: disabled. Reporting each as missing on
    a healthy board is the every-run-red noise that teaches people to stop
    reading the report."""
    config = {"Exposes": [{"Name": "Fan connector 1", "Status": "disabled"},
                          {"Name": "Inlet Temp"}]}
    walk = _walk({"Name": "Inlet Temp", "Reading": 22.0})
    assert compare(_declaration(config), walk).findings == []
    opted_in = compare(_declaration(config), walk, include_disabled_in_config=True)
    assert "declared_absent" in _kinds(opted_in)


def test_a_defect_in_the_declaration_reaches_the_report():
    """A contradiction in the expectation source is invisible to anything that
    only watches readings, so the diff carries it through."""
    config = {"Exposes": [{"Name": "HSC", "Thresholds": [
        {"Direction": "less than", "Label": "temp1", "Name": "upper critical", "Value": 105}]}]}
    walk = _walk({"Name": "HSC", "Reading": 40.0})
    report = compare(_declaration(config), walk)
    assert "threshold_direction_conflict" in _kinds(report)
    assert report.exit_code == 1


def test_a_healthy_board_with_config_disabled_sensors_is_silent():
    """The noise floor, pinned.

    The first cut of `compare` excluded config-disabled sensors from pairing
    rather than from expectation, so their live counterparts came back as
    `undeclared_present` -- four findings on a completely healthy board, every
    single run. A report that is never empty when everything is fine trains its
    reader to stop opening it, and that costs more than the check is worth.
    """
    config = {"Exposes": [
        {"Name": "Fan connector 0", "Status": "disabled"},
        {"Name": "Fan connector 1", "Status": "disabled"},
        {"Name": "Inlet Temp"},
    ]}
    walk = _walk(
        {"Name": "Fan connector 0", "Status": {"State": "Disabled"}},
        {"Name": "Fan connector 1", "Status": {"State": "Disabled"}},
        {"Name": "Inlet Temp", "Reading": 22.0})
    report = compare(_declaration(config), walk)
    assert report.findings == []
    assert report.counts()["undeclared_present"] == 0


def test_config_says_disabled_and_the_machine_reports_it_anyway():
    """The signal the fix above added rather than removed: the configuration and
    the machine disagree about whether that hardware is switched on, and every
    downstream model generator trusts the configuration."""
    config = {"Exposes": [{"Name": "Fan connector 0", "Status": "disabled"}]}
    walk = _walk({"Name": "Fan connector 0", "Reading": 4200,
                  "Status": {"State": "Enabled"}})
    report = compare(_declaration(config), walk)
    assert _kinds(report) == {"disabled_in_config_but_live"}
    assert "4200" in report.findings[0].detail
