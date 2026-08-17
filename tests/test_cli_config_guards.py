"""What every `--config` command must refuse, and what it must not.

Two hazards live here, and they were found the same way -- by someone reading the
published repository, not by this suite.

**Files that parse and declare nothing.** Pointed at the upstream `schemas/` directory,
`declare` printed `read 22 file(s)` with `0 unreadable` and exited 0 -- every number
honest, the answer meaningless, and indistinguishable from a board that genuinely
declares no sensors.

**Files that could not be read at all.** `coverage` and `detect` printed `cannot read:
... every sensor this file declares is unverifiable, not absent` and then exited 0,
which is the one outcome that sentence rules out. The case that matters was in no
report: a real configuration directory with a single corrupt file in it, where
everything else audits normally and the gate goes green.

**That is the shape this tool exists to catch on someone else's machine.** Its own
README argues that a clean result must be distinguishable from one where nothing was
testable; the CLI did not have that property while making the argument.

The command list is DERIVED FROM THE PARSER, and that is the load-bearing part of this
file. The second hazard shipped because a parametrize list here said
`["declare", "coverage"]` while the parser had grown a third `--config` command. A
missing member of a parametrize list is not a failure -- it is a test that silently
never runs, which is strictly worse than the same list written as an equality
assertion, because that at least goes red.

The near-miss tests are the half that keeps the guards usable: a real corpus and a
single real configuration must both stay quiet, or a guard becomes a row that is red
for a legitimate reason and everybody learns to ignore the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WALK = ROOT / "tests" / "fixtures" / "walk_sensors_tree.json"

sys.path.insert(0, str(SRC))

# A real entity-manager configuration: an `Exposes` entry that declares a sensor.
REAL_CONFIG = {"Name": "Board", "Exposes": [{"Name": "Inlet Temp", "Type": "TMP75"}]}

# Valid JSON that declares no sensor -- the shape of a JSON Schema file, which is
# what sits alongside the configurations upstream and is what a reader sweeps up
# by pointing at the repository root instead of `configurations/`.
SCHEMA_SHAPED = {"$schema": "http://json-schema.org/draft-07/schema#",
                 "title": "Thresholds", "type": "object", "properties": {}}

# `detect` exits 2 without the optional extra, for a reason that has nothing to do
# with any guard here. Tests that expect a CLEAN run have to know the difference, or
# they pass for the wrong reason -- which is how the first reproduction of the
# unreadable defect looked like a refutation of it.
try:  # pragma: no cover - environment probe, not a branch under test
    import arbiter_engine  # noqa: F401

    ENGINE_INSTALLED = True
except ImportError:  # pragma: no cover
    ENGINE_INSTALLED = False


def _config_commands() -> list[str]:
    """Every subcommand that takes `--config`, read out of the parser itself.

    Deliberately not a written list. See the module docstring.
    """
    from bmc_sensor_audit.cli import build_parser

    found = []
    for action in build_parser()._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            if any("--config" in (option.option_strings or []) for option in sub._actions):
                found.append(name)
    return sorted(found)


CONFIG_COMMANDS = _config_commands()

# What each command needs BEYOND `--config` in order to run at all. The set above is
# derived; this table is written, so it is pinned against the derived set below. A new
# `--config` command then fails loudly here instead of quietly not being tested.
EXTRA_ARGS = {
    "declare": [],
    "coverage": ["--walk", str(WALK)],
    "detect": ["--walk", str(WALK)],
}


def _env() -> dict:
    """Current environment with PYTHONPATH pointed at src/.

    Not a wholesale replacement: stripping the environment breaks a tool-cache
    interpreter on a CI runner, which is how the first CI run failed while every
    local run passed.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)
    return environment


def _run(command: str, *paths: Path) -> subprocess.CompletedProcess:
    """Run the CLI the way the README prints it, out of process, so the exit code
    under test is the one a shell would see."""
    argv = [sys.executable, "-m", "bmc_sensor_audit.cli", command]
    for path in paths:
        argv += ["--config", str(path)]
    argv += EXTRA_ARGS[command]
    return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          env=_env())


def _declare(*paths: Path) -> subprocess.CompletedProcess:
    return _run("declare", *paths)


def _write(directory: Path, name: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(json.dumps(payload))
    return target


def _write_broken(directory: Path, name: str = "broken.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("{ not json at all")
    return target


class TestTheCommandSetIsDerived:
    def test_the_extra_args_table_covers_every_config_command(self):
        """The pin that makes the derivation useful. If the parser grows a fifth
        command taking --config, this is the row that goes red, and it names the
        command that is missing rather than leaving a silent gap in the parametrize
        lists below."""
        assert sorted(EXTRA_ARGS) == CONFIG_COMMANDS, (
            "the parser and this table disagree about which commands take --config; "
            f"parser says {CONFIG_COMMANDS}, table says {sorted(EXTRA_ARGS)}")

    def test_more_than_one_command_is_covered(self):
        """Guards against the derivation silently returning nothing -- an empty
        parametrize list is green, and would hide every test in this file."""
        assert len(CONFIG_COMMANDS) >= 3


class TestUnreadableIsIncompleteNotClean:
    """An unreadable config is not a clean board; it is an unknown one."""

    @pytest.mark.parametrize("command", CONFIG_COMMANDS)
    def test_a_wholly_unreadable_path_is_incomplete(self, command, tmp_path):
        result = _run(command, tmp_path / "no-such-file.json")
        assert result.returncode == 2, (
            f"{command} returned {result.returncode} for a config it could not read:\n"
            f"{result.stdout}\n{result.stderr}")

    @pytest.mark.parametrize("command", CONFIG_COMMANDS)
    def test_one_corrupt_file_among_good_ones_is_incomplete(self, command, tmp_path):
        """The case that matters, and the one no report named.

        A real configuration directory with a single unparseable file. Everything else
        audits normally, so every other signal in the run says clean.
        """
        _write(tmp_path, "board.json", REAL_CONFIG)
        _write_broken(tmp_path)
        result = _run(command, tmp_path)
        assert result.returncode == 2, (
            f"{command} returned {result.returncode} with one corrupt config among "
            f"readable ones:\n{result.stdout}\n{result.stderr}")

    @pytest.mark.parametrize("command", ["coverage", "detect"])
    def test_the_refusal_says_why_and_not_only_what(self, command, tmp_path):
        """Discriminating, not merely passing.

        The file's NAME is not enough to prove the guard ran: it already appears in the
        Stage 1 report as an informational finding, with the defect present and absent
        alike. Asserting only the name passed against the shipped defect when it was
        planted back deliberately -- which makes it evidence of nothing. `detect` also
        exits 2 with no engine extra installed, for a reason unrelated to any guard
        here. So this asserts the sentence that only the refusal emits.
        """
        _write(tmp_path, "board.json", REAL_CONFIG)
        broken = _write_broken(tmp_path)
        result = _run(command, tmp_path)
        combined = result.stdout + result.stderr
        assert "cannot report a clean board" in combined, (
            f"{command} exited 2 without the refusal ever being reached:\n{combined}")
        assert broken.name in combined, (
            f"{command} refused without naming the unreadable file:\n{combined}")

    @pytest.mark.parametrize("command", ["coverage", "detect"])
    def test_the_report_survives_the_refusal(self, command, tmp_path):
        """The refusal must not replace the report.

        Mirrors the same rule for `declare`: a reader needs the part that WAS readable,
        both to see how much of the board was covered and to tell a corrupt file apart
        from a wrong path. Bailing out before the comparison would have been the
        smaller change and would have cost exactly this.
        """
        _write(tmp_path, "board.json", REAL_CONFIG)
        _write_broken(tmp_path)
        result = _run(command, tmp_path)
        # Collapsed, because these are column-aligned counts and the assertion would
        # otherwise be measuring the padding.
        collapsed = " ".join(result.stdout.split())
        assert "declared 1" in collapsed and "matched 1" in collapsed, (
            f"{command} dropped the readable half of the report:\n{result.stdout}")


class TestTheDeclarationFreeHazard:
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
        files WERE read -- that is the fact that tells them the path was not the
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

    @pytest.mark.parametrize("command", CONFIG_COMMANDS)
    def test_every_command_refuses_a_declaration_free_path_the_same_way(
            self, command, tmp_path):
        """The original defect was that they disagreed. Pin the agreement so a future
        change to one of them cannot silently re-open the gap."""
        _write(tmp_path, "schema.json", SCHEMA_SHAPED)
        result = _run(command, tmp_path)
        assert result.returncode == 2, f"{command} returned {result.returncode}"


class TestTheNearMisses:
    """The noise floor. If a guard fires on legitimate input, the exit code stops
    being read at all -- which costs more than the guard is worth."""

    @pytest.mark.parametrize("command", CONFIG_COMMANDS)
    def test_a_real_configuration_is_clean(self, command, tmp_path):
        if command == "detect" and not ENGINE_INSTALLED:
            pytest.skip("detect exits 2 without the optional extra, unrelated to any "
                        "guard here; the canary workflow installs it and runs this")
        _write(tmp_path, "board.json", REAL_CONFIG)
        result = _run(command, tmp_path)
        assert result.returncode == 0, f"{command}: {result.stdout}\n{result.stderr}"

    def test_a_real_configuration_beside_schema_files_is_clean(self, tmp_path):
        """The mixed directory. One declaring file is enough -- the guard asks
        whether anything was declared, never whether everything was."""
        _write(tmp_path, "board.json", REAL_CONFIG)
        _write(tmp_path, "schema.json", SCHEMA_SHAPED)
        result = _declare(tmp_path)
        assert result.returncode == 0, result.stderr

    def test_the_vendored_corpus_stays_quiet(self):
        """A real corpus, not a fixture. If the guards fire on the nine upstream
        configurations this repository vendors, they are wrong."""
        result = _declare(ROOT / "tests" / "fixtures" / "upstream")
        assert result.returncode == 0, result.stderr


class TestExitCodesAreTheContract:
    def test_the_three_codes_are_distinct(self):
        """`coverage` documents 0/1/2 as the CI interface. Every command shares the
        vocabulary, so a caller reading any of them gets the same meanings."""
        from bmc_sensor_audit.cli import EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE
        assert (EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE) == (0, 1, 2)
