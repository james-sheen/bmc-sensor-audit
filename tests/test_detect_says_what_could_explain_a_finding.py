"""`detect` prints the engine's ranking of declared causes beside a finding.

A finding says what is wrong; the operator's next question is what could have
caused it, and the engine answers that from the causes the model declares. The
Mt. Jade file declares one -- the zone's fan can fail into its ambient reading --
with no strength, so the honest ranking names the fan and says why it carries no
number. A clean run prints nothing new: the section exists only where there is a
finding to explain.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bmc_sensor_audit import cli

ROOT = Path(__file__).resolve().parents[1]
MTJADE = ROOT / "tests" / "fixtures" / "upstream" / "ampere" / "mtjade.json"
BOARD_FILE = ROOT / "examples" / "supplemental" / "ampere-mtjade.json"
HEADER = "What could explain it"


def _engine():
    return pytest.importorskip(
        "arbiter_engine.api", reason="the ranking is an engine capability")


def _walks(tmp: Path, ambient: tuple[float, ...]) -> list[str]:
    paths = []
    for step, reading in enumerate(ambient):
        def point(name, value, units, thresholds):
            return {"name": name, "path": f"/redfish/v1/Chassis/mtjade/Sensors/{name}",
                    "reading": value, "units": units, "state": "Enabled",
                    "health": "OK", "shape": "sensors", "thresholds": thresholds}
        walk = {"format": "bmc-sensor-audit/walk/1",
                "chassis": ["/redfish/v1/Chassis/mtjade"], "shapes_seen": ["sensors"],
                "errors": [],
                "sensors": [point("TS4_Temp", reading, "Cel", {"upper/critical": 50.0}),
                            point("FAN3_1", 3000.0 + step, "RPM",
                                  {"upper/critical": 23100.0, "lower/critical": 500.0})]}
        path = tmp / f"walk{step}.json"
        path.write_text(json.dumps(walk))
        paths.append(str(path))
    return paths


def _detect(tmp_path, capsys, ambient):
    argv = ["detect", "--config", str(MTJADE), "--supplemental", str(BOARD_FILE)]
    for path in _walks(tmp_path, ambient):
        argv += ["--walk", path]
    cli.main(argv)
    return capsys.readouterr().out


def test_a_finding_is_printed_with_the_causes_the_model_declares(tmp_path, capsys):
    _engine()
    out = _detect(tmp_path, capsys, (55.0, 56.0))
    assert HEADER in out, out[-1500:]
    ranking = out[out.index(HEADER):]
    assert re.search(r"^  TS4_Temp: FAN3_1", ranking, re.M), ranking


def test_an_unscored_cause_says_why_in_a_name_a_reader_can_look_up(tmp_path, capsys):
    """The channel carries no strength, so the fan comes back without a posterior,
    and what is printed beside it is the engine's own decline name."""
    _engine()
    from arbiter_engine.subenvelope import VOCABULARIES
    from arbiter_engine.types import NotEvaluatedReason

    published = {r.value for r in NotEvaluatedReason}.union(*VOCABULARIES.values())
    out = _detect(tmp_path, capsys, (55.0, 56.0))
    ranking = out[out.index(HEADER):]
    assert re.search(r"^  TS4_Temp: FAN3_1 \(unranked: cpt_missing\)$", ranking,
                     re.M), ranking
    for reasons in re.findall(r"\(unranked: ([^)]*)\)", ranking):
        assert set(reasons.split(", ")) <= published, reasons


def test_a_clean_run_prints_no_ranking(tmp_path, capsys):
    _engine()
    out = _detect(tmp_path, capsys, (40.0, 41.0))
    assert HEADER not in out


def test_a_resident_run_prints_it_with_the_first_report_only(tmp_path, capsys,
                                                             monkeypatch):
    """A resident run prints the full report once, on its first cycle, and a
    line per cycle after it. The ranking belongs to the report: every cycle
    here is over the bound, and it is printed once."""
    _engine()
    from datetime import datetime, timedelta, timezone

    from bmc_sensor_audit.inventory.redfish import walk_from_dict

    walks = [walk_from_dict(json.loads(Path(path).read_text()))
             for path in _walks(tmp_path, (55.0, 56.0, 57.0))]
    clock = {"at": datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)}

    def now():
        at = clock["at"]
        clock["at"] = at + timedelta(seconds=60)
        return at
    monkeypatch.setattr(cli, "walk_chassis", lambda _client: walks.pop(0))
    monkeypatch.setattr(cli, "_client", lambda _args: None)
    monkeypatch.setattr(cli, "_now", now)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
    cli.main(["detect", "--config", str(MTJADE), "--supplemental", str(BOARD_FILE),
              "--target", "https://bench.invalid", "--resident",
              "--ledger", str(tmp_path / "ledger.sqlite"), "--cycles", "3"])
    out = capsys.readouterr().out
    assert walks == [], "the run stopped before its third cycle"
    assert out.count(HEADER) == 1, out[-1500:]
    assert re.search(r"^  TS4_Temp: FAN3_1", out, re.M), out[-1500:]
