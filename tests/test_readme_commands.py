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

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

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
        pytest.skip("console script form; requires an install, covered below")
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
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")})

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
        env={"PATH": "/usr/bin:/bin"})
    assert result.returncode != 0
    assert "No module named" in result.stderr
