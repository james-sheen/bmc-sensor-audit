"""Run the commands the README prints, exactly as it prints them.

This exists because the README was wrong the moment it was written, and stayed
wrong through a full test suite. It said *no installation required to run it from
a checkout* and gave a bare `python3 -m bmc_sensor_audit.cli`. The package lives
under `src/`, so from a fresh clone that command raises ModuleNotFoundError. Every
run during development had `PYTHONPATH=src` already set, so the documented command
was never the command being tested.

The general shape is worth naming, because it is the one this project is built to
catch in other systems: **a check written from the author's vocabulary inherits
the author's blind spot.** A test that restates the quickstart in its own words
passes forever while the printed quickstart is broken. So this test READS the
commands out of the README rather than restating them, and runs them from a
directory containing no checkout.

If you change the README's command block, this runs whatever you changed it to.
That is the point.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def _env(**overrides) -> dict:
    """The current environment with PYTHONPATH removed, plus any overrides.

    These tests used to pass `env={"PATH": "/usr/bin:/bin"}`, replacing the
    environment wholesale. That is invisible locally, where `/usr/bin/python3`
    needs nothing else to start — and it broke on the first CI run, because a
    tool-cache interpreter installed by `setup-python` relies on variables the
    wholesale replacement threw away.

    The intent was only ever *PYTHONPATH is not set*. Express that, and leave the
    rest of the environment alone.
    """
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment.update(overrides)
    return environment

# Placeholders the README uses for things a reader must supply. A command
# carrying one cannot be run here; it is checked for shape instead.
PLACEHOLDER = re.compile(r"<[^>]+>")


def _command_lines() -> list[str]:
    blocks = re.findall(r"```\n(.*?)```", README.read_text(), re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if "bmc_sensor_audit.cli" in line
            or line.startswith("bmc-sensor-audit")]


def test_the_readme_prints_commands_at_all():
    """A guard against this whole file passing vacuously. If the block is
    renamed or reformatted away, every test below would find nothing to run and
    report success."""
    assert len(_command_lines()) >= 4


@pytest.mark.parametrize("line", _command_lines())
def test_every_documented_command_parses(line):
    """The argument parser accepts each documented invocation. Placeholders are
    substituted with values that exist, so a typo in a flag name fails here."""
    if line.startswith("bmc-sensor-audit"):
        pytest.skip("console script form; covered by TestTheConsoleScript")
    argv = line.split("bmc_sensor_audit.cli", 1)[1].split()
    argv = ["/dev/null" if PLACEHOLDER.fullmatch(a) else a for a in argv]

    sys.path.insert(0, str(ROOT / "src"))
    from bmc_sensor_audit.cli import build_parser
    parsed = build_parser().parse_args(argv)
    assert parsed.command in {"declare", "coverage", "capture"}


def test_the_documented_invocation_runs_from_a_directory_with_no_checkout(tmp_path):
    """The failure that prompted this file. Run the README's own module command
    from somewhere with no repository in it, the way a reader would.

    `cwd=tmp_path` is load-bearing: run from the repository root, `src/` is not
    on the path either, but any stray build artefact or editable install could
    make it work by accident. From a temp directory only the documented
    PYTHONPATH can be doing the work.
    """
    config = tmp_path / "board.json"
    config.write_text('{"Exposes": [{"Name": "Inlet Temp", "Type": "TMP75"}]}')

    line = next(line for line in _command_lines() if " declare " in line)
    assert line.startswith("PYTHONPATH=src"), (
        "the documented command no longer sets PYTHONPATH; if the layout changed, "
        "this test needs to change with it rather than be deleted")

    argv = line.split("bmc_sensor_audit.cli", 1)[1].split()
    argv = [str(config) if PLACEHOLDER.fullmatch(a) else a for a in argv]

    result = subprocess.run(
        [sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
        cwd=tmp_path, capture_output=True, text=True,
        env=_env(PYTHONPATH=str(ROOT / "src")))

    assert result.returncode == 0, result.stderr
    assert "sensors declared" in result.stdout


def test_the_bare_command_without_pythonpath_still_fails(tmp_path):
    """The paired negative, and the reason the line above is not decoration.

    Without this, the test above passes just as well if the package were
    importable for some unrelated reason, and the README could quietly go back
    to printing a command that does not run.
    """
    result = subprocess.run(
        [sys.executable, "-m", "bmc_sensor_audit.cli", "--help"],
        cwd=tmp_path, capture_output=True, text=True,
        env=_env())
    assert result.returncode != 0
    assert "No module named" in result.stderr


class TestTheConsoleScript:
    """The README's second documented path, which nothing covered.

    The skip above used to read *covered below* and nothing below covered it — a
    false statement inside a test, which is the same family as the defect this
    whole file exists for. This class makes the claim true.

    **Its limit, stated rather than implied**: these assertions do not run `pip`.
    They catch the realistic breakages — a renamed entry point, a script name that
    drifted from the one the README prints, a target that no longer resolves — and
    they do not catch packaging mechanics. **That gap is now closed elsewhere**:
    `.github/workflows/checks.yml` installs the package on a clean runner and runs
    the console script from a directory with no checkout, on every push. Here the
    checks stay structural so every developer does not pay for a pip install on
    every run; there they are real, because a clean runner is free.
    """

    def test_the_readme_and_pyproject_agree_on_the_script_name(self):
        """A rename in one file and not the other prints a command that does not
        exist. Read out of both files rather than restated in a literal here."""
        declared = re.search(r"^\s*([\w-]+)\s*=\s*\"bmc_sensor_audit\.cli:main\"",
                             (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
        assert declared, "pyproject declares no console script pointing at cli:main"
        printed = [line.split()[0] for line in _command_lines()
                   if not line.startswith("PYTHONPATH")]
        assert printed, "the README prints no console-script invocation"
        assert set(printed) == {declared.group(1)}, (
            f"README prints {set(printed)}, pyproject declares {declared.group(1)!r}")

    def test_the_declared_entry_point_resolves_to_a_callable(self):
        """`cli:main` must exist and be callable. An entry point naming a function
        that was renamed installs cleanly and fails on first use."""
        sys.path.insert(0, str(ROOT / "src"))
        from bmc_sensor_audit import cli
        assert callable(getattr(cli, "main", None)), \
            "pyproject points at bmc_sensor_audit.cli:main, which is not callable"

    def test_the_readme_says_the_console_script_needs_no_pythonpath(self):
        """The reason the second path is documented at all. If this sentence goes,
        the two blocks become an unexplained duplicate."""
        assert "no `PYTHONPATH`" in README.read_text()


class TestTheReadmeTestCount:
    """The README states a test count, and a number written in two places drifts.

    It was published saying **84** while the suite was 91, and reached 104 before
    anyone read it — false on the public surface the whole time, because nothing
    compared them. A count in prose has no owner; this gives it one.

    The alternative was deleting the number. It is kept because it is the one line
    telling a reader the project is tested at all, and a claim worth making is
    worth pinning.
    """

    def test_the_readme_count_matches_what_pytest_collects(self):
        """Measured on the DEPENDENCY-FREE suite, which is a population that does not
        depend on what happens to be installed.

        The optional `[detect]` extra adds eight canary tests, so without this the
        figure differs by whether the engine is present — 161 against 169 — and the
        check goes red for a legitimate reason on any machine that has the extra. A
        row like that teaches people to skip the whole check, which costs more than
        the check is worth.
        """
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", str(ROOT / "tests"),
             "--collect-only", "-q", "-p", "no:cacheprovider",
             "--ignore", str(ROOT / "tests" / "test_engine_bridge.py")],
            cwd=str(ROOT), capture_output=True, text=True)
        found = re.search(r"(\d+) tests? collected", collected.stdout)
        assert found, f"could not read a collection count:\n{collected.stdout[-400:]}"

        claimed = re.search(r"\|\s*Tests\s*\|\s*(\d+)", README.read_text())
        assert claimed, "the README Status table no longer states a test count"

        assert int(claimed.group(1)) == int(found.group(1)), (
            f"README claims {claimed.group(1)} tests, pytest collects {found.group(1)} "
            "— update the README in the same change that added or removed tests")
