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
import json
import shutil
import importlib.metadata
import importlib.util
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
    parser = build_parser()
    parsed = parser.parse_args(argv)

    # The valid set is read OUT OF the parser, never restated here. Restating it
    # made this test fail the day a fourth subcommand was added -- not because the
    # command was wrong, but because the test carried a copy of a list that had
    # moved. A checker built from the author's vocabulary inherits the author's
    # blind spot, which is the failure this whole file exists to catch elsewhere.
    known = {name for action in parser._subparsers._group_actions
             for name in action.choices}
    assert parsed.command in known, f"{parsed.command} is not a subcommand: {known}"


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


def _installed_as_a_distribution() -> bool:
    """Whether pip put this package in the environment, as opposed to pytest
    putting `src/` on `sys.path` via `pythonpath` in `pyproject.toml`.

    THE PREDICATE IS THE WHOLE DIFFICULTY and the first one was wrong. It asked
    whether `src/` was absent, which is true of an installed wheel and FALSE of
    an unpacked sdist -- and the sdist is the artifact somebody actually runs.
    So the skip did not fire there and the test failed for exactly the reason it
    had just been taught to skip for. Measured, not reasoned: 705 passed, this
    one red, from a venv holding the sdist.

    Asking *can a bare command import it* would be asking the assertion, and a
    skip computed from the assertion never fires when it matters. Distribution
    metadata is a fact about the environment and is decided before the
    subprocess runs.
    """
    try:
        importlib.metadata.version("bmc-sensor-audit")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


@pytest.mark.skipif(
    _installed_as_a_distribution(),
    reason="the package is installed in this environment, so a bare command "
           "correctly succeeds and this negative cannot hold. Skipped with a "
           "reason rather than failed: the claim is about a checkout where the "
           "only copy is the one in the tree, and it says nothing about the "
           "README when there is a second copy on the path.")
def test_the_bare_command_without_pythonpath_still_fails(tmp_path):
    """The paired negative, and the reason the line above is not decoration.

    Without this, the test above passes just as well if the package were
    importable for some unrelated reason, and the README could quietly go back
    to printing a command that does not run.

    SCOPED 2026-09-02: the claim is *without `pythonpath`, from a checkout*. An
    installed package makes it false by design, which is a different world and
    not a regression -- reported from outside after running this suite from the
    sdist, where it fails for a reason that says nothing about the README.

    The first scoping was wrong and is recorded in `_installed_as_a_distribution`
    above: it keyed on the absence of `src/`, which an sdist ships.
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

    @staticmethod
    def _tracked_tests() -> list[str]:
        """The test files this repository has, asked of git rather than the disk.

        Falls back to the directory when git cannot answer -- because an empty list
        would collect nothing and report a confident zero, and a zero is the most
        dangerous wrong count there is.

        Git can fail to answer in two different ways and only one of them is a
        return code. A checkout with no `.git` exits non-zero; an image with no git
        BINARY raises instead, and `python:3.11-slim` is exactly that image. Catching
        only the first would have turned this test into an error on any environment
        that runs the suite without git installed.
        """
        try:
            listed = subprocess.run(["git", "ls-files", "--", "tests/test_*.py"],
                                    cwd=str(ROOT), capture_output=True, text=True)
        except OSError:
            return [str(ROOT / "tests")]
        paths = [line for line in listed.stdout.split() if line]
        if listed.returncode != 0 or not paths:
            return [str(ROOT / "tests")]
        # The engine-bridge exclusion is applied HERE and not left to `--ignore`.
        # `--ignore` filters directory collection; it does not suppress a file named
        # explicitly on the command line, so once this returns paths instead of a
        # directory the flag stops covering it. Measured: the count read 292 with the
        # engine installed and 272 without -- the exact installed-dependent population
        # this test exists to avoid, reintroduced by the mechanism meant to fix it.
        return [str(ROOT / path) for path in paths
                if not path.endswith("test_engine_bridge.py")]

    #: A directory placed at the front of `PYTHONPATH` whose `sitecustomize`
    #: makes `import yaml` fail. `site` imports it at interpreter start, before
    #: pytest collects anything, so a module-level `importorskip` sees exactly
    #: what it would see on a machine with nothing installed.
    #:
    #: Blocking rather than uninstalling: the alternative is a second virtual
    #: environment per run, and a check nobody can afford to run is a check
    #: that gets deleted.
    _BLOCK_YAML = "import sys\nsys.modules['yaml'] = None\n"

    def _collect(self, tmp_path=None, *, with_yaml: bool) -> int:
        env = dict(os.environ)
        if not with_yaml:
            shim = tmp_path / "noyaml"
            shim.mkdir(exist_ok=True)
            (shim / "sitecustomize.py").write_text(self._BLOCK_YAML)
            env["PYTHONPATH"] = os.pathsep.join(
                [str(shim)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", *self._tracked_tests(),
             "--collect-only", "-q", "-p", "no:cacheprovider",
             "--ignore", str(ROOT / "tests" / "test_engine_bridge.py")],
            cwd=str(ROOT), capture_output=True, text=True, env=env)
        found = re.search(r"(\d+) tests? collected", collected.stdout)
        assert found, f"could not read a collection count:\n{collected.stdout[-400:]}"
        return int(found.group(1))

    @staticmethod
    def _claimed() -> list[int]:
        """Every number in the Tests row, in order. The row states TWO
        populations and both are claims, so both are read."""
        row = re.search(r"\|\s*Tests\s*\|([^|]*)\|", README.read_text())
        assert row, "the README Status table no longer has a Tests row"
        numbers = [int(n) for n in re.findall(r"\d+", row.group(1))]
        assert len(numbers) >= 2, (
            f"the Tests row states {len(numbers)} number(s); it is supposed to "
            f"state the collected count with PyYAML and without it, because "
            f"they differ and a single number cannot be true of both")
        return numbers

    def test_the_readme_count_matches_what_pytest_collects(self, tmp_path):
        """The first number: PyYAML present, which is what CI runs.

        This docstring used to name two specific figures and both had gone
        stale -- the defect this test exists to catch, one layer in.
        """
        pytest.importorskip(
            "yaml", reason="PyYAML is not installed, so the with-PyYAML "
                           "population could not be measured here. The "
                           "dependency-free number below still is")
        assert self._claimed()[0] == self._collect(tmp_path, with_yaml=True), (
            "the README's first test count and pytest disagree -- update the "
            "README in the same change that added or removed tests")

    def test_the_dependency_free_count_matches_too(self, tmp_path):
        """The second number, and **the one the README used to get wrong**.

        The row read *691 collected with no dependencies installed*. Measured
        with nothing installed it is 671: the qualifier was off by the twenty
        tests that read YAML. The figure had an owner -- this test -- and the
        sentence around it did not, so the number stayed true and the claim
        about it was false wherever PyYAML was absent, which is every
        environment the sentence was describing.

        A qualifier is part of a claim. Pinning the number and leaving the
        qualifier in prose pins the half that was already right.
        """
        assert self._claimed()[1] == self._collect(tmp_path, with_yaml=False), (
            "the README's dependency-free test count and pytest disagree")

    def test_the_two_populations_differ_by_the_module_the_readme_names(
            self, tmp_path):
        """The README says the difference is exactly one module. That is a
        claim about WHICH tests, not how many, and a count cannot check it.

        Also the non-vacuity of the pair above: if the shim stopped working,
        both numbers would measure the same population and agree forever.
        """
        pytest.importorskip("yaml", reason="both populations need to differ "
                                           "before the difference can be named")
        with_yaml, without = self._collect(tmp_path, with_yaml=True), \
            self._collect(tmp_path, with_yaml=False)
        assert with_yaml > without, (
            "blocking PyYAML changed nothing, so the shim is not working and "
            "the dependency-free count above is measuring the wrong thing")

        # Scoped to the Tests ROW, not to the whole file. A search over the
        # README finds the first backticked test path anywhere in it, which is
        # a different module and would compare an unrelated count -- a check
        # reporting on its own regex rather than on the claim it was aimed at.
        row = re.search(r"\|\s*Tests\s*\|([^|]*)\|", README.read_text())
        assert row, "the README Status table no longer has a Tests row"
        named = re.findall(r"`tests/(test_\w+\.py)`", row.group(1))
        assert named, ("the Tests row no longer names the module that makes "
                       "the gap between the two populations")
        listed = subprocess.run(
            [sys.executable, "-m", "pytest", str(ROOT / "tests" / named[0]),
             "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=str(ROOT), capture_output=True, text=True)
        found = re.search(r"(\d+) tests? collected", listed.stdout)
        assert found and int(found.group(1)) == with_yaml - without, (
            f"the README names {named[0]} as the whole difference between the "
            f"two populations, and it accounts for "
            f"{found.group(1) if found else 'no'} of {with_yaml - without}")


class TestTheDocumentedMockBlockRuns:
    """The README's Python block, executed as printed.

    Extracted from the file rather than restated here: a copy in this test is a
    copy that can drift, and the whole point is that the text a reader runs is
    the text that was checked. Rewriting the command into something equivalent
    would test the rewriter -- which is how a broken quickstart survived four
    releases of a sibling package.
    """

    @staticmethod
    def _block() -> str:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        section = text.split("## A machine to test against, without a machine", 1)
        assert len(section) == 2, "the mock section is gone from the README"
        body = section[1]
        start = body.index("```python") + len("```python")
        return body[start:body.index("```", start)]

    def test_the_block_names_the_public_surface(self):
        """Runs everywhere, including where the console script is absent."""
        block = self._block()
        for name in ("MockBMC", "MockSensor", "serve"):
            assert name in block, f"the documented block no longer shows {name}"

    def test_every_name_the_prose_promises_exists(self):
        """The bullets below the block are claims about an API."""
        from bmc_sensor_audit.testing import mock_redfish
        import inspect
        for name in ("MockBMC", "MockSensor", "serve"):
            assert hasattr(mock_redfish, name), f"{name} is documented and absent"
        for name in ("add", "remove", "disable"):
            assert hasattr(mock_redfish.MockBMC, name), (
                f"MockBMC.{name} is documented and absent")
        parameters = inspect.signature(mock_redfish.MockBMC).parameters
        for name in ("sensors", "shape", "fail", "etags"):
            assert name in parameters, (
                f"MockBMC({name}=...) is documented and is not a parameter")

    def test_the_block_actually_runs(self, tmp_path):
        # The block needs BOTH halves, and the first version of this guard
        # checked one: the console script on PATH, and the package importable by
        # the interpreter that runs the block. With only the first it ran and
        # died on `No module named bmc_sensor_audit`, which is the environment
        # failing rather than the documentation being wrong.
        importable = subprocess.run(
            [sys.executable, "-c", "import bmc_sensor_audit"],
            capture_output=True).returncode == 0
        if shutil.which("bmc-sensor-audit") is None or not importable:
            pytest.skip("the documented block needs the console script on PATH "
                        "and the package importable here; one is missing, so "
                        "the block was never executed and nothing was checked")
        script = tmp_path / "block.py"
        script.write_text(self._block(), encoding="utf-8")
        done = subprocess.run([sys.executable, str(script)],
                              cwd=str(tmp_path), capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert "OUTCOME walked" in done.stdout
        walk = json.loads((tmp_path / "walk.json").read_text())
        assert len(walk["sensors"]) == 2, "the block's two sensors did not arrive"