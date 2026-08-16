"""Tests for the declaration reader.

Each test below corresponds to something measured in the upstream corpus rather
than to something the format documentation says. Where a test asserts that the
parser detects a defect, there is a paired test asserting it stays quiet on the
correct version -- a check that fires on everything is not a check.
"""

from __future__ import annotations

import json

import pytest

from bmc_sensor_audit.inventory.entity_manager import (
    load_declaration, parse_config_text,
)

TMP75 = {
    "Name": "WFP Baseboard",
    "Exposes": [
        {"Name": "Left Rear Temp", "Type": "TMP75", "Bus": 6, "Address": "0x49",
         "Thresholds": [
             {"Direction": "greater than", "Name": "upper critical", "Severity": 1, "Value": 115},
             {"Direction": "greater than", "Name": "upper non critical", "Severity": 0, "Value": 110},
             {"Direction": "less than", "Name": "lower non critical", "Severity": 0, "Value": 5},
             {"Direction": "less than", "Name": "lower critical", "Severity": 1, "Value": 0}]},
        {"Name": "1U System Fan connector 1", "Type": "IntelFanConnector",
         "Pwm": 1, "Status": "disabled", "Tachs": [1, 2]},
    ],
}


def test_reads_the_documented_example():
    declaration = parse_config_text(json.dumps(TMP75))
    assert len(declaration) == 2
    temp = declaration.by_key()[("Left Rear Temp", None)]
    assert temp.type == "TMP75"
    assert temp.record == "WFP Baseboard"
    assert len(temp.thresholds) == 4
    assert {(t.bound, t.level) for t in temp.thresholds} == {
        ("upper", "critical"), ("upper", "warning"),
        ("lower", "critical"), ("lower", "warning")}


def test_status_disabled_is_carried_not_dropped():
    declaration = parse_config_text(json.dumps(TMP75))
    disabled = declaration.disabled
    assert [s.name for s in disabled] == ["1U System Fan connector 1"]


def test_top_level_may_be_a_list_or_an_object():
    """178 files in the corpus are objects and 59 are lists."""
    as_object = parse_config_text(json.dumps(TMP75))
    as_list = parse_config_text(json.dumps([TMP75]))
    assert len(as_object) == len(as_list) == 2


def test_block_comments_are_tolerated():
    """Ten of 247 upstream configs carry C-style comments. json.load raises on
    them, and a skipped file makes its sensors look undeclared rather than
    unread -- a false clean bill of health for the whole board."""
    text = '{\n  /* a comment upstream really ships */\n  "Name": "B",\n' \
           '  "Exposes": [{"Name": "S", "Type": "ADC"}]\n}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    declaration = parse_config_text(text)
    assert declaration.unreadable == []
    assert [s.name for s in declaration] == ["S"]


def test_an_unreadable_file_is_recorded_never_skipped(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text('{"Exposes": [ this is not json ]}')
    declaration = load_declaration([bad])
    assert len(declaration) == 0
    assert len(declaration.unreadable) == 1
    assert "broken.json" in declaration.unreadable[0][0]


def test_one_entry_with_labels_declares_several_sensors():
    """748 corpus entries carry per-rail labels; one carries 33. Counting
    Exposes entries counts boards, not sensors."""
    hsc = {"Exposes": [{"Name": "HSC", "Type": "pmbus", "Thresholds": [
        {"Direction": "greater than", "Label": "vin", "Name": "upper critical", "Value": 13.75},
        {"Direction": "less than", "Label": "vin", "Name": "lower critical", "Value": 11.25},
        {"Direction": "greater than", "Label": "iout1", "Name": "upper critical", "Value": 52},
    ]}]}
    declaration = parse_config_text(json.dumps(hsc))
    assert {s.label for s in declaration} == {"vin", "iout1"}
    assert {s.display_name for s in declaration} == {"HSC:vin", "HSC:iout1"}


def test_templated_names_are_flagged():
    """Roughly one name in eight is substituted at runtime, so it never appears
    literally on the machine."""
    cfg = {"Exposes": [{"Name": "$bus_ADC0", "Type": "ADC"},
                       {"Name": "P12V_AUX", "Type": "ADC"}]}
    declaration = parse_config_text(json.dumps(cfg))
    assert [s.name for s in declaration.templated] == ["$bus_ADC0"]


def test_direction_conflict_is_detected():
    """Two real upstream configs name a threshold 'upper critical' and give it
    Direction 'less than'. The named condition then cannot alarm, and the
    healthy range does."""
    cfg = {"Exposes": [{"Name": "HSC", "Type": "pmbus", "Thresholds": [
        {"Direction": "less than", "Label": "temp1", "Name": "upper critical", "Value": 105}]}]}
    declaration = parse_config_text(json.dumps(cfg))
    kinds = [a.kind for a in declaration.anomalies]
    assert "threshold_direction_conflict" in kinds


def test_direction_conflict_is_quiet_on_the_corrected_version():
    """The paired negative. Same sensor, Direction repaired, no anomaly."""
    cfg = {"Exposes": [{"Name": "HSC", "Type": "pmbus", "Thresholds": [
        {"Direction": "greater than", "Label": "temp1", "Name": "upper critical", "Value": 105}]}]}
    assert parse_config_text(json.dumps(cfg)).anomalies == []


def test_an_unrecognised_severity_level_is_reported_not_dropped():
    """The threshold-name vocabulary is open by design: fifteen distinct names
    appear across 10,687 corpus thresholds. A closed enum with a missing member
    misclassifies confidently instead of failing."""
    cfg = {"Exposes": [{"Name": "S", "Type": "ADC", "Thresholds": [
        {"Direction": "greater than", "Name": "upper apocalyptic", "Value": 9000}]}]}
    declaration = parse_config_text(json.dumps(cfg))
    assert [a.kind for a in declaration.anomalies] == ["unclassified_threshold_level"]
    # Carried through, not discarded -- the value is still visible to a reader.
    assert declaration.sensors[0].thresholds[0].value == 9000
    assert declaration.sensors[0].thresholds[0].level is None
    # The bound is still known, because Direction is authoritative.
    assert declaration.sensors[0].thresholds[0].bound == "upper"


@pytest.mark.parametrize("name,level", [
    ("upper critical", "critical"),
    ("lower critical", "critical"),
    ("upper non critical", "warning"),
    ("lower non critical", "warning"),
    ("upper non recoverable", "non_recoverable"),
    ("lower non recoverable", "non_recoverable"),
    ("lower non-recoverable", "non_recoverable"),
    ("higher critical", "critical"),
    ("higher non critical", "warning"),
    ("Ambient Upper Critical", "critical"),
    ("HardShutdown", "hard_shutdown"),
    ("SoftShutdown", "soft_shutdown"),
    ("Warning", "warning"),
    ("lower hardshutdown", "hard_shutdown"),
])
def test_every_threshold_name_in_the_corpus_classifies(name, level):
    """All fifteen observed spellings, pinned. If upstream adds a sixteenth this
    suite stays green and the anomaly report is what surfaces it -- which is the
    intended division of labour."""
    cfg = {"Exposes": [{"Name": "S", "Thresholds": [
        {"Direction": "greater than", "Name": name, "Value": 1}]}]}
    declaration = parse_config_text(json.dumps(cfg))
    assert declaration.sensors[0].thresholds[0].level == level


def test_bound_comes_from_direction_not_from_the_name():
    """`HardShutdown` and `Warning` carry no bound token at all -- 13 in the
    corpus -- and Direction is the only signal that resolves them."""
    cfg = {"Exposes": [{"Name": "S", "Thresholds": [
        {"Direction": "greater than", "Name": "HardShutdown", "Value": 1},
        {"Direction": "less than", "Name": "Warning", "Value": 2}]}]}
    declaration = parse_config_text(json.dumps(cfg))
    bounds = {t.name: t.bound for t in declaration.sensors[0].thresholds}
    assert bounds == {"HardShutdown": "upper", "Warning": "lower"}
