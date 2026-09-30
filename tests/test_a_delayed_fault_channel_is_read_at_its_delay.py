"""A fault channel that says how long a failure takes to show is read that much
earlier, and only over walks placed by their own times.

presence-audit 0.2.6 reads `propagation_delay_s` on a fault channel and writes
it where the engine reads a dead time on a causal edge, so the cause is read
that long before its finding -- a fan that stopped a minute before the zone ran
hot is caught even if it is spinning again by then. Placed on the declared
grid, a recorded walk's instant is its place on a ladder ending at the clock,
and the engine would read the fan at a time nobody walked it: `detect --walk`
refuses a delayed channel without `--by-capture-time`, as cases already do.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bmc_sensor_audit import cli

ROOT = Path(__file__).resolve().parents[1]
MTJADE = ROOT / "tests" / "fixtures" / "upstream" / "ampere" / "mtjade.json"
BOARD_FILE = ROOT / "examples" / "supplemental" / "ampere-mtjade.json"
TEMP, FAN = "TS4_Temp", "FAN3_1"
START = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
#: The ambient reading at each walk, 60 s apart: under its bound, then over.
AMBIENT = (40.0, 55.0)


def _engine():
    return pytest.importorskip("arbiter_engine.api",
                               reason="a ranking is an engine capability")


def _delayed_board(tmp: Path, delay: float = 60.0) -> Path:
    """The shipped board file with a delay on its one fault channel: a fixture's
    number, not a claim about this board. None of its files states one."""
    document = json.loads(BOARD_FILE.read_text())
    document["fault_channels"][0]["propagation_delay_s"] = delay
    path = tmp / "delayed.json"
    path.write_text(json.dumps(document))
    return path


def _walks(tmp: Path) -> list[str]:
    paths = []
    for step, reading in enumerate(AMBIENT):
        def point(name, value, units, thresholds):
            return {"name": name, "path": f"/redfish/v1/Chassis/mtjade/Sensors/{name}",
                    "reading": value, "units": units, "state": "Enabled",
                    "health": "OK", "shape": "sensors", "thresholds": thresholds}
        walk = {"format": "bmc-sensor-audit/walk/1",
                "chassis": ["/redfish/v1/Chassis/mtjade"], "shapes_seen": ["sensors"],
                "errors": [],
                "captured_at": (START + timedelta(seconds=60 * step)).isoformat(
                    timespec="seconds"),
                "sensors": [point(TEMP, reading, "Cel", {"upper/critical": 50.0}),
                            point(FAN, 3000.0 + step, "RPM",
                                  {"upper/critical": 23100.0, "lower/critical": 500.0})]}
        path = tmp / f"walk{step}.json"
        path.write_text(json.dumps(walk))
        paths.append(str(path))
    return paths


def _detect(capsys, board, walks, *extra):
    argv = ["detect", "--config", str(MTJADE), "--supplemental", str(board), *extra]
    for path in walks:
        argv += ["--walk", path]
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestRecordedWalksNeedTheirOwnTimes:

    def test_without_the_flag_a_delayed_channel_is_refused(self, tmp_path, capsys):
        _engine()
        code, _out, err = _detect(capsys, _delayed_board(tmp_path), _walks(tmp_path))
        assert code == cli.EXIT_INCOMPLETE
        assert f"{FAN} -> {TEMP}" in err and "--by-capture-time" in err

    def test_with_it_the_run_goes_ahead(self, tmp_path, capsys):
        _engine()
        code, out, err = _detect(capsys, _delayed_board(tmp_path), _walks(tmp_path),
                                 "--by-capture-time")
        assert code != cli.EXIT_INCOMPLETE, err
        assert "Placed by capture time" in out

    def test_a_board_that_declares_no_delay_is_not_asked_for_it(self, tmp_path, capsys):
        _engine()
        code, out, err = _detect(capsys, BOARD_FILE, _walks(tmp_path))
        assert "declares a delay" not in err
        # ...and says where the walks were placed instead.
        assert "Placed on the declared grid" in out

    def test_a_zero_delay_moves_nothing_and_asks_for_nothing(self, tmp_path, capsys):
        _engine()
        code, _out, err = _detect(capsys, _delayed_board(tmp_path, 0.0), _walks(tmp_path))
        # The run goes ahead: refused for any reason, this would pass on a core
        # that cannot read the key at all.
        assert code != cli.EXIT_INCOMPLETE, err
        assert "declares a delay" not in err


def test_the_engine_reads_the_fan_a_delay_before_the_zone_ran_hot(tmp_path):
    """Each cycle fed at its own instant, as a resident run feeds one: the zone
    is over its bound at the second walk, and the fan is read one delay earlier."""
    api = _engine()
    import yaml
    from arbiter_engine import InMemoryObservationHistory

    from bmc_sensor_audit.inventory.entity_manager import load_declaration
    from bmc_sensor_audit.inventory.redfish import walk_from_dict
    from presence_audit.diff import compare
    from presence_audit.feeder import feed
    from presence_audit.generator import generate
    from presence_audit.supplemental import load_supplemental

    declaration = load_declaration([str(MTJADE)])
    model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                               supplemental=load_supplemental(str(_delayed_board(tmp_path))))
    [channel] = [r for r in model["domain"]["relationship_rules"]
                 if r.get("edge_direction") == "causal"]
    assert channel["temporal"] == {"propagation_delay_s": 60.0}
    model_path = tmp_path / "model.yaml"
    model_path.write_text(yaml.safe_dump(model))

    history = InMemoryObservationHistory()
    for step, path in enumerate(_walks(tmp_path)):
        at = START + timedelta(seconds=60 * step)
        with api.as_of(at):
            session = api.EngineSession(history=history)
            session.load_model(str(model_path))
            walk = walk_from_dict(json.loads(Path(path).read_text()))
            feed(session, manifest, [compare(declaration, walk)])
            api.check(session)
            leg = api.hypothesize(session, TEMP).to_dict()["hypothesis"]
    [fan] = [row for row in leg["candidates"] if row["cause"] == FAN]
    assert fan["read_at"] == [START.replace(tzinfo=None).isoformat()]
