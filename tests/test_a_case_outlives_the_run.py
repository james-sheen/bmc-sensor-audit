"""A finding becomes a case the next walk is checked into, and a person can say
what settled it.

The engine keeps a case book in the ledger's file, and nothing here opened one:
a finding lived for one run, and no command could record the cause a person
later confirmed. `--case-severity` and `--case-checks` declare when a case
closes -- the operator's number, because the board file cannot carry it -- and
with them each finding opens a case in the `--ledger` file, the ranking is
attached, and each later walk is checked into it. `confirm` records the cause
and the reading that settled it; `cases` prints the book.

A case counts walks, so each is judged into the book once: recorded walks need
`--by-capture-time`, and a run the book has already judged is refused.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bmc_sensor_audit import cli

ROOT = Path(__file__).resolve().parents[1]
MTJADE = ROOT / "tests" / "fixtures" / "upstream" / "ampere" / "mtjade.json"
BOARD_FILE = ROOT / "examples" / "supplemental" / "ampere-mtjade.json"
#: In the past, as a recorded walk is: a confirmation is filed at the clock, after
#: the ranking it is read against.
START = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
#: The ambient reading at each walk, 60 s apart: over the bound twice, then under.
AMBIENT = (55.0, 56.0, 40.0, 41.0)
CASES = ["--case-severity", "warning", "--case-checks", "2"]


def _engine():
    return pytest.importorskip("arbiter_engine.api",
                               reason="a case is an engine capability")


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
                "sensors": [point("TS4_Temp", reading, "Cel", {"upper/critical": 50.0}),
                            point("FAN3_1", 3000.0 + step, "RPM",
                                  {"upper/critical": 23100.0, "lower/critical": 500.0})]}
        path = tmp / f"walk{step}.json"
        path.write_text(json.dumps(walk))
        paths.append(str(path))
    return paths


def _detect(capsys, walks, *extra):
    argv = ["detect", "--config", str(MTJADE), "--supplemental", str(BOARD_FILE),
            "--by-capture-time", *extra]
    for path in walks:
        argv += ["--walk", path]
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _case_id(out: str) -> str:
    [case_id] = re.findall(r"^  (\S+)  TS4_Temp\.\S+", out, re.M)
    return case_id


class TestACaseOutlivesTheRun:

    def test_a_finding_opens_a_case_that_two_clean_walks_close(self, tmp_path, capsys):
        _engine()
        walks, ledger = _walks(tmp_path), str(tmp_path / "ledger.db")
        _code, out, _err = _detect(capsys, walks[:2], "--ledger", ledger, *CASES)
        assert "cases: 1 opened, 1 open, 0 resolved" in out, out[-1500:]
        case_id = _case_id(out)
        _detect(capsys, walks[:3], "--ledger", ledger, *CASES)
        _code, out, _err = _detect(capsys, walks, "--ledger", ledger, *CASES)
        assert "0 opened, 0 open, 1 resolved" in out, out[-1500:]
        assert cli.main(["cases", "--ledger", ledger]) == 0
        listing = capsys.readouterr().out
        assert re.search(rf"^  {case_id}  TS4_Temp\.\S+  resolved  .*checks: "
                         r"clean, clean$", listing, re.M), listing

    def test_each_walk_is_judged_into_the_book_once(self, tmp_path, capsys):
        """The same walks again would record the same reading as a second clean
        check, and close a case on half the evidence."""
        _engine()
        walks, ledger = _walks(tmp_path), str(tmp_path / "ledger.db")
        _code, out, _err = _detect(capsys, walks[:2], "--ledger", ledger, *CASES)
        assert "1 opened" in out, "no case opened, so a repeat could count nothing"
        code, _out, err = _detect(capsys, walks[:2], "--ledger", ledger, *CASES)
        assert code == cli.EXIT_INCOMPLETE
        assert "a case counts each walk once" in err

    def test_the_ranking_names_the_reading_to_take_first(self, tmp_path, capsys):
        _engine()
        _code, out, _err = _detect(capsys, _walks(tmp_path)[:2])
        assert re.search(r"^  TS4_Temp: FAN3_1 \(unranked: cpt_missing; own reading: [^)]+\)$\n"
                         r"^    walk: open -- still open: FAN3_1, .*$\n"
                         r"^    read first: FAN3_1\.reading -- the only declared cause$",
                         out, re.M), out[-1500:]


class TestAPersonSaysWhatSettledIt:

    def test_the_confirmation_is_read_back_against_the_ranking(self, tmp_path, capsys):
        _engine()
        walks, ledger = _walks(tmp_path), str(tmp_path / "ledger.db")
        _code, out, _err = _detect(capsys, walks[:2], "--ledger", ledger, *CASES)
        case_id = _case_id(out)
        code = cli.main(["confirm", "--ledger", ledger, case_id, "--cause", "FAN3_1",
                         "--reading", "FAN3_1.reading", "--basis", "a bench check"])
        assert code == cli.EXIT_CLEAN
        assert capsys.readouterr().out.strip() == (
            f"case {case_id}: FAN3_1 ranked 1 of 1 (by its standing on the walk, "
            f"not by posterior); settled by FAN3_1.reading, the reading the ranking "
            f"named")
        cli.main(["cases", "--ledger", ledger, "--json"])
        confirmed = json.loads(capsys.readouterr().out)["confirmed"]
        assert (confirmed["confirmations"], confirmed["settling_reading_was_named"]) \
            == (1, 1)

    def test_an_unknown_case_is_refused_by_name(self, tmp_path, capsys):
        _engine()
        walks, ledger = _walks(tmp_path), str(tmp_path / "ledger.db")
        _detect(capsys, walks[:2], "--ledger", ledger, *CASES)
        code = cli.main(["confirm", "--ledger", ledger, "no-such-case",
                         "--cause", "FAN3_1", "--basis", "a guess"])
        assert code == cli.EXIT_INCOMPLETE
        assert "not recorded -- malformed_request" in capsys.readouterr().err

    def test_a_ledger_that_is_not_there_is_refused_not_created(self, tmp_path, capsys):
        missing = tmp_path / "typo.db"
        code = cli.main(["confirm", "--ledger", str(missing), "any",
                         "--cause", "FAN3_1", "--basis", "a guess"])
        assert code == cli.EXIT_INCOMPLETE
        assert "there is no ledger" in capsys.readouterr().err
        assert not missing.exists()


class TestTheFlagsAreRefusedWhereTheyCouldNotKeepACase:

    @pytest.mark.parametrize("extra, said", [
        (["--case-severity", "warning"], "Give both, or neither"),
        (["--case-checks", "2"], "Give both, or neither"),
        (["--case-severity", "warning", "--case-checks", "0"], "at least 1"),
        (CASES, "add --ledger PATH"),
    ])
    def test_refused(self, tmp_path, capsys, extra, said):
        code, _out, err = _detect(capsys, _walks(tmp_path)[:2], *extra)
        assert code == cli.EXIT_INCOMPLETE and said in err, err

    def test_recorded_walks_need_their_own_times(self, tmp_path, capsys):
        argv = ["detect", "--config", str(MTJADE), "--ledger",
                str(tmp_path / "ledger.db"), *CASES]
        for path in _walks(tmp_path)[:2]:
            argv += ["--walk", path]
        assert cli.main(argv) == cli.EXIT_INCOMPLETE
        assert "add --by-capture-time" in capsys.readouterr().err


class TestAResidentRunKeepsCasesEveryCycle:

    def test_the_cases_ride_the_first_report_and_every_cycle_line(
            self, tmp_path, capsys, monkeypatch):
        _engine()
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        walks = [walk_from_dict(json.loads(Path(path).read_text()))
                 for path in _walks(tmp_path)]
        clock = {"at": START}

        def now():
            at = clock["at"]
            clock["at"] = at + timedelta(seconds=60)
            return at
        monkeypatch.setattr(cli, "walk_chassis", lambda _client: walks.pop(0))
        monkeypatch.setattr(cli, "_client", lambda _args: None)
        monkeypatch.setattr(cli, "_now", now)
        monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
        ledger = str(tmp_path / "ledger.db")
        cli.main(["detect", "--config", str(MTJADE), "--supplemental", str(BOARD_FILE),
                  "--target", "https://bmc.invalid", "--resident", "--cycles", "4",
                  "--ledger", ledger, *CASES])
        out = capsys.readouterr().out
        assert out.count("cases: 1 opened, 1 open, 0 resolved") == 1, out[-2000:]
        cycles = re.findall(r"^cycle \d+ at .*$", out, re.M)
        assert len(cycles) == 4 and all("; cases " in line for line in cycles), cycles
        assert "0 opened, 0 open, 1 resolved" in cycles[-1], cycles[-1]
