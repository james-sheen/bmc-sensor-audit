"""A run over nothing is refused or said, never answered clean.

Measured against every input this package reads, an empty or nameless one got
through in four places:

* **a walk whose sensor has no name** raised `KeyError` out of `coverage`,
  `detect`, `regression` and `adopt` -- a traceback exits `1`, which this
  family reads as FINDINGS -- while `validate-walk` named the same fault;
* **two walks holding no sensor** compared as *No changes. Every sensor
  reported before is reported now*, exit `0`;
* **an `Exposes` entry with no name** was dropped with no anomaly, so a board
  whose entries lost their names read as a smaller board;
* **an attestation recording zero entities checked** validated.

Each is refused here, in this package, so it holds on every core this package
admits -- not only on a core that learned the same rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bmc_sensor_audit import cli
from bmc_sensor_audit.inventory.entity_manager import parse_config_text
from bmc_sensor_audit.inventory.redfish import (WALK_FORMAT, WalkFileError,
                                                walk_from_dict)

SENSOR = {"name": "INLET_TEMP", "path": "/redfish/v1/Chassis/1/Sensors/INLET_TEMP",
          "reading": 24.0, "units": "Cel", "state": "Enabled", "health": "OK",
          "shape": "sensors"}


def _walk(*sensors) -> dict:
    return {"format": WALK_FORMAT, "sensors": list(sensors), "errors": [],
            "chassis": ["/redfish/v1/Chassis/1"], "shapes_seen": ["sensors"]}


def _write(path: Path, payload) -> str:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return str(path)


@pytest.fixture
def board(tmp_path) -> str:
    return _write(tmp_path / "board.json",
                  {"Exposes": [{"Name": "INLET_TEMP", "Type": "TMP75"}]})


@pytest.fixture
def nameless(tmp_path) -> str:
    return _write(tmp_path / "nameless.json",
                  _walk(SENSOR, {"reading": 30.0, "units": "Cel"}))


class TestAWalkTheReaderCannotReadIsRefusedByName:

    @pytest.mark.parametrize("command", ["coverage", "detect"])
    def test_a_sensor_with_no_name(self, command, board, nameless, capsys):
        assert cli.main([command, "--config", board, "--walk", nameless]) == 2
        err = capsys.readouterr().err
        assert "sensors[1] carries no 'name'" in err
        assert nameless in err

    def test_regression_refuses_it_on_either_side(self, tmp_path, nameless, capsys):
        whole = _write(tmp_path / "whole.json", _walk(SENSOR))
        assert cli.main(["regression", "--before", whole, "--after", nameless]) == 2
        assert "carries no 'name'" in capsys.readouterr().err
        assert cli.main(["regression", "--before", nameless, "--after", whole]) == 2

    def test_adopt_refuses_it(self, tmp_path, board, nameless, capsys):
        # Per test, not per module: `adopt` refuses a missing engine before it
        # reads a walk, and a module-level skip would move this file out of one
        # of the README's two collected populations and not the other.
        pytest.importorskip("arbiter_engine.api",
                            reason="adopt asks for the engine before the walk")
        two = _write(tmp_path / "two.json", {"Exposes": [
            {"Name": "INLET_TEMP", "Type": "TMP75"},
            {"Name": "FAN_TACH", "Type": "TMP75"}]})
        supplemental = _write(tmp_path / "supplemental.json", {
            "format": "presence-audit/supplemental/2",
            "provenance": "a fixture; states nothing about any board",
            "sampling_interval_s": 60.0,
            "couplings": [{"from": "FAN_TACH", "to": "INLET_TEMP",
                           "propagation_delay_s": 60.0, "time_constant_s": 60.0,
                           "response_model": "step", "gain": "estimate",
                           "basis": "a fixture"}]})
        code = cli.main(["adopt", "--config", two, "--supplemental", supplemental,
                         "--walk", nameless, "--list"])
        assert code == 2
        assert "carries no 'name'" in capsys.readouterr().err

    @pytest.mark.parametrize("content,said", [
        (None, "cannot read the walk"),
        ("{ not json", "is not parseable as JSON"),
        ("[1, 2]", "not an object"),
    ])
    def test_a_file_that_is_not_a_walk(self, tmp_path, board, content, said, capsys):
        path = tmp_path / "walk.json"
        if content is not None:
            path.write_text(content)
        assert cli.main(["coverage", "--config", board, "--walk", str(path)]) == 2
        assert said in capsys.readouterr().err

    def test_the_reader_names_every_nameless_sensor_at_once(self):
        with pytest.raises(WalkFileError, match=r"sensors\[0\] and 1 more"):
            walk_from_dict(_walk({"reading": 1.0}, SENSOR, {"name": ""}))

    def test_a_walk_with_names_reads_as_before(self):
        assert [s.name for s in walk_from_dict(_walk(SENSOR)).sensors] == ["INLET_TEMP"]

    def test_the_refusal_is_one_main_turns_into_two(self):
        assert WalkFileError in cli.REFUSALS


class TestTwoEmptyWalksCompareAsNothing:

    def test_refused_rather_than_reported_clean(self, tmp_path, capsys):
        a = _write(tmp_path / "a.json", _walk())
        b = _write(tmp_path / "b.json", _walk())
        assert cli.main(["regression", "--before", a, "--after", b]) == 2
        captured = capsys.readouterr()
        assert "neither" in captured.err and "holds a single sensor" in captured.err
        assert "reported before is reported now" not in captured.out

    def test_a_board_that_lost_every_sensor_is_still_a_regression(self, tmp_path):
        a = _write(tmp_path / "a.json", _walk(SENSOR))
        b = _write(tmp_path / "b.json", _walk())
        assert cli.main(["regression", "--before", a, "--after", b]) == 1

    def test_an_unchanged_board_still_reads_clean(self, tmp_path, capsys):
        a = _write(tmp_path / "a.json", _walk(SENSOR))
        b = _write(tmp_path / "b.json", _walk(SENSOR))
        assert cli.main(["regression", "--before", a, "--after", b]) == 0
        assert "No changes" in capsys.readouterr().out


class TestAnEntryWithNoNameIsAnAnomaly:

    CONFIG = {"Exposes": [{"Name": "INLET_TEMP", "Type": "TMP75"},
                          {"Type": "TMP75"},
                          {"Name": "", "Type": "TMP75"}]}

    def test_each_is_said_and_none_is_counted(self):
        declaration = parse_config_text(json.dumps(self.CONFIG), "board.json")
        assert [s.display_name for s in declaration.sensors] == ["INLET_TEMP"]
        assert [a.kind for a in declaration.anomalies] == ["unnamed_entry"] * 2
        assert "TMP75" in declaration.anomalies[0].detail

    def test_declare_prints_them(self, tmp_path, capsys):
        board = _write(tmp_path / "board.json", self.CONFIG)
        cli.main(["declare", "--config", board])
        out = capsys.readouterr().out
        assert re.search(r"anomalies\s+2\n", out), out
        assert out.count("unnamed_entry") == 2


def _artifact(**checked) -> dict:
    return {"format": "presence-audit/attestation/2", "target": "t",
            "engine": {"schema_version": 1, "boundary": None},
            "checked": dict(checked), "findings": [], "not_checked": [],
            "evidence": [], "unattested": [], "unread_feeds": []}


class TestAnAttestationOfNothing:

    def test_zero_entities_checked_is_refused_once(self, tmp_path, capsys):
        path = _write(tmp_path / "a.json", _artifact(invariants=0, entities=0))
        assert cli.main(["validate-attestation", path]) == 1
        err = capsys.readouterr().err
        assert err.count("checked.entities is 0") == 1, err

    def test_a_run_over_entities_is_valid(self, tmp_path):
        path = _write(tmp_path / "a.json", _artifact(invariants=12, entities=4))
        assert cli.main(["validate-attestation", path]) == 0
