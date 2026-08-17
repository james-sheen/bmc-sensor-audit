"""`declare` must not report a clean parse of files that declare nothing.

Found by a post-push read of the published repository, not by the suite and not by
the hygiene hook. Pointed at the upstream `schemas/` directory, `declare` printed
`read 22 file(s)` with `0 unreadable` and exited 0 — every number honest, the
answer meaningless, and indistinguishable from a board that genuinely declares no
sensors. `coverage` already refused exactly this case with exit 2.

**That is the shape this tool exists to catch on someone else's machine.** Its own
README argues that a clean result must be distinguishable from one where nothing
was testable; `declare` did not have that property while making the argument.

The near-miss tests are the half that keeps the guard usable: a real corpus and a
single real configuration must both stay quiet, or the guard becomes a row that is
red for a legitimate reason and everybody learns to ignore the exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# A real entity-manager configuration: an `Exposes` entry that declares a sensor.
REAL_CONFIG = {"Name": "Board", "Exposes": [{"Name": "Inlet Temp", "Type": "TMP75"}]}

# Valid JSON that declares no sensor — the shape of a JSON Schema file, which is
# what sits alongside the configurations upstream and is what a reader sweeps up
# by pointing at the repository root instead of `configurations/`.
SCHEMA_SHAPED = {"$schema": "http://json-schema.org/draft-07/schema#",
                 "title": "Thresholds", "type": "object", "properties": {}}


def _declare(*paths: Path) -> subprocess.CompletedProcess:
    """Run the CLI the way the README prints it, out of process, so the exit code
    under test is the one a shell would see."""
    argv = [sys.executable, "-m", "bmc_sensor_audit.cli", "declare"]
    for path in paths:
        argv += ["--config", str(path)]
    return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          env=_env())


def _env() -> dict:
    """Current environment with PYTHONPATH pointed at src/.

    Not a wholesale replacement: stripping the environment breaks a tool-cache
    interpreter on a CI runner, which is how the first CI run failed while every
    local run passed.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    return environment


def _write(directory: Path, name: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(json.dumps(payload))
    return target


class TestTheHazard:
    """Files that parse and declare nothing."""

    def test_a_directory_of_schema_shaped_files_is_incomplete_not_clean(self, tmp_path):
        for index in range(3):
            _write(tmp_path / "schemas", f"s{index}.json", SCHEMA_SHAPED)
        result = _declare(tmp_path / "schemas")
        assert result.returncode == 2, (
            "declare reported exit "
            f"{result.returncode} for files that declare nothing:\n{result.stdout}")
        assert "none of which declares a sensor" in result.stderr

    def test_the_summary_is_still_printed(self, tmp_path):
        """The refusal must not replace the report. A reader needs to see that 3
        files WERE read — that is the fact that tells them the path was not the
        problem, and the kind of directory was."""
        for index in range(3):
            _write(tmp_path / "schemas", f"s{index}.json", SCHEMA_SHAPED)
        result = _declare(tmp_path / "schemas")
        assert "read 3 file(s)" in result.stdout
        assert "sensors declared" in result.stdout

    def test_an_empty_directory_says_something_different(self, tmp_path):
        """Split deliberately: no files read usually means a wrong path, while
        files read that declare nothing usually means the wrong kind of directory.
        One message for both would send the reader to the wrong fix."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _declare(empty)
        assert result.returncode == 2
        assert "no files were read" in result.stderr
        assert "none of which declares" not in result.stderr


class TestTheNearMisses:
    """The noise floor. If the guard fires on legitimate input, the exit code
    stops being read at all — which costs more than the guard is worth."""

    def test_a_single_real_configuration_is_clean(self, tmp_path):
        _write(tmp_path, "board.json", REAL_CONFIG)
        result = _declare(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "sensors declared" in result.stdout

    def test_a_real_configuration_beside_schema_files_is_clean(self, tmp_path):
        """The mixed directory. One declaring file is enough — the guard asks
        whether anything was declared, never whether everything was."""
        _write(tmp_path, "board.json", REAL_CONFIG)
        _write(tmp_path, "schema.json", SCHEMA_SHAPED)
        result = _declare(tmp_path)
        assert result.returncode == 0, result.stderr

    def test_an_unreadable_file_still_reports_incomplete(self, tmp_path):
        """Pre-existing behaviour that must survive: an unparseable config is an
        unknown board, not a clean one. This guard must not have displaced it."""
        _write(tmp_path, "board.json", REAL_CONFIG)
        (tmp_path / "broken.json").write_text("{ not json at all")
        result = _declare(tmp_path)
        assert result.returncode == 2
        assert "unreadable files" in result.stdout


class TestExitCodesAreTheContract:
    def test_the_three_codes_are_distinct(self):
        """`coverage` documents 0/1/2 as the CI interface. `declare` shares the
        vocabulary, so a caller reading either gets the same meanings."""
        sys.path.insert(0, str(SRC))
        from bmc_sensor_audit.cli import EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE
        assert (EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE) == (0, 1, 2)

    @pytest.mark.parametrize("command", ["declare", "coverage"])
    def test_both_commands_refuse_a_declaration_free_path_the_same_way(
            self, command, tmp_path):
        """The defect was that they disagreed. Pin the agreement so a future
        change to one of them cannot silently re-open the gap."""
        _write(tmp_path, "schema.json", SCHEMA_SHAPED)
        argv = [sys.executable, "-m", "bmc_sensor_audit.cli", command,
                "--config", str(tmp_path)]
        if command == "coverage":
            argv += ["--walk", str(ROOT / "tests" / "fixtures" / "walk_sensors_tree.json")]
        result = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                                env=_env())
        assert result.returncode == 2, f"{command} returned {result.returncode}"
