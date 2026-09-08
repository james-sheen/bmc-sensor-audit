"""CI does not install this package, so it must install what the package needs.

The version matrix and the engine canary both run the suite against the source
tree with the package DELIBERATELY not installed -- a test asserting that a bare
module path fails without `PYTHONPATH` is only a real negative that way.

That was free while this project had no dependencies. The split moved the
domain-neutral half to `presence-audit`, the vertical imports it at module
scope, and the whole suite stopped collecting on a runner while passing locally,
because a local editable install had quietly supplied it. Every one of the five
interpreters failed identically and none of them said anything a local run
could have predicted.

So these assert the two halves that have to stay true together: the workflows
install the runtime dependencies, and they get that list from the file that
declares it rather than from a copy.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

sys.path.insert(0, str(ROOT / "tools"))
import runtime_deps                                     # noqa: E402


def _install_lines(path):
    """Lines that RUN a pip install, not lines that mention one.

    The first version of this matched any line containing the words, and caught
    a comment in `checks.yml` explaining what an editable install cannot see.
    A predicate that is not the claim, in a test written to catch a predicate
    that was not its claim.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "pip install" in stripped:
            out.append(line)
    return out


class TestTheHelperReadsTheRealList:
    def test_it_returns_the_declared_runtime_dependencies(self):
        deps = runtime_deps.runtime_dependencies(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert deps, "no runtime dependencies found; the parser or the file moved"
        assert any(d.startswith("presence-audit") for d in deps), deps

    def test_it_excludes_the_optional_groups(self):
        """An extra is by definition something the package runs without.
        Installing one here would hide a missing guard instead of revealing it."""
        deps = runtime_deps.runtime_dependencies(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert not any("arbiter-engine" in d or "pytest" in d for d in deps), deps

    def test_the_fallback_parser_agrees_with_tomllib(self):
        """The matrix starts at 3.10 and `tomllib` is 3.11+, so the regex path
        is the one that runs on the oldest interpreter -- and a fallback that
        disagreed with the real parser would be worse than no fallback.

        Skipped where there is no `tomllib` to compare against, which is the
        3.10 leg -- the very interpreter the fallback exists for. That is not
        circular: the fallback is EXERCISED there by every other test in this
        file, and only the agreement check needs both parsers present.
        """
        tomllib = pytest.importorskip(
            "tomllib", reason="3.11+; the fallback is exercised here regardless")
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        via_toml = list(tomllib.loads(text)["project"]["dependencies"])
        import re
        block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
        via_regex = re.findall(r'"([^"]+)"', block.group(1))
        assert via_regex == via_toml, (via_regex, via_toml)

    def test_it_runs_as_a_command(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "runtime_deps.py")],
                             capture_output=True, text=True, cwd=str(ROOT))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip(), "printed nothing; the workflow would install nothing"


class TestEveryJobThatRunsTheSuiteInstallsThem:
    """The population is every install line that is followed by a test run and
    does NOT install the package itself. Named by file so a new workflow has to
    be added here deliberately rather than inherit a pass by being unseen."""

    @pytest.mark.parametrize("name", ["checks.yml", "canary.yml"])
    def test_the_workflow_derives_the_list(self, name):
        path = WORKFLOWS / name
        assert path.is_file(), f"{name} is gone; this test now measures nothing"
        installs = [l for l in _install_lines(path) if "-e ." not in l and "build" not in l]
        assert installs, f"no suite-install line found in {name}"
        for line in installs:
            assert "runtime_deps.py" in line, (
                f"{name} installs test tooling but not the package's own runtime "
                f"dependencies, so the suite cannot collect:\n  {line.strip()}")

    def test_no_workflow_hardcodes_the_dependency_name(self):
        """A copy of the range is a copy that drifts from the one that binds,
        and the drift is invisible until the two disagree."""
        for path in WORKFLOWS.glob("*.yml"):
            for line in _install_lines(path):
                assert "presence-audit" not in line, (
                    f"{path.name} names the dependency literally; derive it "
                    f"from pyproject instead:\n  {line.strip()}")
