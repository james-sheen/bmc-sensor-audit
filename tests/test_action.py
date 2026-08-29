"""The composite action, exercised without a runner.

`action.yml` is a bash script embedded in YAML, and nothing about that script needs
GitHub to be tested: give it the environment a runner would, put a stub named
`bmc-sensor-audit` in front of it on PATH, and read back the argv it built and the
outputs it wrote. What is under test is the ACTION -- the translation from inputs a
consumer writes to flags the tool receives -- not the tool, which the rest of this
suite covers.

The reason to test it here rather than only in a workflow: a workflow proves the
action works on the three fixtures CI happens to run. These cases cover the
combinations a consumer will reach that CI will not, including the two refusals and
the credential path, which needs no BMC to check the argv is right.

**The injection case is the one to keep.** Every input reaches bash through `env:`
rather than `${{ }}`, because a workflow expression is substituted into the script
before bash parses it. The specification this was built from sketched
`--config "${{ inputs.config }}"`, which executes whatever a caller puts in that
field. This asserts the shape that does not.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="action.yml is YAML; CI installs PyYAML")

from packaging.requirements import Requirement  # noqa: E402

from bmc_sensor_audit import __version__  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"
README = ROOT / "README.md"

ACTION_SPEC = yaml.safe_load(ACTION.read_text())
AUDIT_SCRIPT = next(s for s in ACTION_SPEC["runs"]["steps"]
                    if s.get("id") == "audit")["run"]
#: The install step has no `id`; it is named. Kept separate from the audit
#: script because what it decides -- which tool version a consumer gets --
#: is a different question from the argv the audit step builds.
INSTALL_SCRIPT = next(s for s in ACTION_SPEC["runs"]["steps"]
                      if s.get("name") == "Install the pinned tool")["run"]

BASE = {"BSA_MODE": "detect", "BSA_CONFIG": "configs/", "BSA_WALK": "",
        "BSA_TARGET": "", "BSA_USERNAME": "", "BSA_PASSWORD": "",
        "BSA_INSECURE": "false", "BSA_ATTEST": "false", "BSA_ATTEST_LABEL": ""}


class Result:
    def __init__(self, argv, outputs, log, returncode):
        self.argv, self.outputs, self.log, self.returncode = argv, outputs, log, returncode


def run_action(tmp_path: Path, env: dict, tool_exit: int = 0) -> Result:
    """Run the audit step with a stub tool, and report what it did."""
    binder = tmp_path / "bin"
    binder.mkdir()
    argv_file = tmp_path / "argv.txt"
    stub = binder / "bmc-sensor-audit"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {argv_file}\nexit {tool_exit}\n')
    stub.chmod(0o755)
    output_file = tmp_path / "gh_output"
    output_file.write_text("")

    proc = subprocess.run(
        ["bash", "-c", AUDIT_SCRIPT], cwd=str(tmp_path), capture_output=True, text=True,
        env={**os.environ, "PATH": f"{binder}{os.pathsep}{os.environ['PATH']}",
             "GITHUB_OUTPUT": str(output_file), **BASE, **env})

    argv = argv_file.read_text().splitlines() if argv_file.exists() else None
    outputs = dict(line.split("=", 1) for line in output_file.read_text().splitlines()
                   if "=" in line)
    return Result(argv, outputs, proc.stdout + proc.stderr, proc.returncode)


class TestTheDocumentedCaseBuildsTheDocumentedCommand:
    def test_the_five_line_form(self, tmp_path):
        r = run_action(tmp_path, {"BSA_WALK": "captures/after.json"})
        assert r.argv == ["detect", "--config", "configs/",
                          "--walk", "captures/after.json"]
        assert r.returncode == 0

    def test_several_walks_keep_the_order_they_were_given(self, tmp_path):
        """Liveness reads walks as a series and the CLI documents oldest first, so
        an action that reordered them would produce a confident wrong answer."""
        r = run_action(tmp_path, {"BSA_WALK": "a.json\nb.json\nc.json"})
        assert r.argv == ["detect", "--config", "configs/", "--walk", "a.json",
                          "--walk", "b.json", "--walk", "c.json"]

    def test_several_config_paths_each_become_their_own_flag(self, tmp_path):
        r = run_action(tmp_path, {"BSA_CONFIG": "meta/\nextra/board.json",
                                  "BSA_WALK": "w.json"})
        assert r.argv == ["detect", "--config", "meta/", "--config", "extra/board.json",
                          "--walk", "w.json"]

    def test_a_live_target_carries_its_credentials(self, tmp_path):
        r = run_action(tmp_path, {"BSA_TARGET": "https://bmc.example",
                                  "BSA_USERNAME": "root", "BSA_PASSWORD": "s3cret",
                                  "BSA_INSECURE": "true"})
        assert r.argv == ["detect", "--config", "configs/",
                          "--target", "https://bmc.example",
                          "--username", "root", "--password", "s3cret", "--insecure"]


class TestTheExitContractSurvivesTheActionLayer:
    """The tool's three exit codes are its CI interface. An action that collapsed
    them would undo the distinction at the last hop, where nobody would look."""

    @pytest.mark.parametrize("code,verdict", [(0, "clean"), (1, "regressions"),
                                              (2, "incomplete")])
    def test_the_code_and_its_word_are_both_reported(self, tmp_path, code, verdict):
        r = run_action(tmp_path, {"BSA_WALK": "w.json"}, tool_exit=code)
        assert r.returncode == code
        assert r.outputs["exit-code"] == str(code)
        assert r.outputs["verdict"] == verdict

    def test_the_outputs_are_written_even_when_the_step_fails(self, tmp_path):
        """Written before the exit on purpose. A step that dies without writing its
        outputs leaves a consumer branching on an empty string, and the runs worth
        branching on are exactly the ones that failed."""
        r = run_action(tmp_path, {"BSA_WALK": "w.json"}, tool_exit=1)
        assert r.returncode != 0
        assert r.outputs == {"exit-code": "1", "verdict": "regressions"}


class TestAMisconfiguredRunReportsIncompleteAndNotClean:
    """It has not judged the machine it was pointed at. The whole argument for a
    distinct 2 is that nothing else may read as 0."""

    def test_walk_and_target_together(self, tmp_path):
        r = run_action(tmp_path, {"BSA_WALK": "w.json",
                                  "BSA_TARGET": "https://bmc.example"})
        assert r.returncode == 2
        assert r.outputs["verdict"] == "incomplete"
        assert r.argv is None, "the tool should not have been run at all"

    def test_neither_walk_nor_target(self, tmp_path):
        r = run_action(tmp_path, {})
        assert r.returncode == 2
        assert r.outputs["verdict"] == "incomplete"

    def test_attest_without_detect(self, tmp_path):
        """`--attest-out` exists on `detect` and not on `coverage`. Passing it
        anyway would surface as argparse's own usage error, which says nothing
        about the input the caller actually got wrong."""
        r = run_action(tmp_path, {"BSA_MODE": "coverage", "BSA_WALK": "w.json",
                                  "BSA_ATTEST": "true"})
        assert r.returncode == 2
        assert "attest requires mode: detect" in r.log
        assert r.argv is None


class TestTheAttestationWarning:
    """An uploaded artifact is a different door out of the pipeline than the log,
    and no secret scanner reads it. A warning rather than a refusal, because the
    caller may have meant it."""

    def test_a_live_target_with_no_label_warns(self, tmp_path):
        r = run_action(tmp_path, {"BSA_TARGET": "https://bmc.example",
                                  "BSA_ATTEST": "true"})
        assert "::warning::" in r.log
        assert "--attest-out" in r.argv

    def test_a_label_silences_it(self, tmp_path):
        r = run_action(tmp_path, {"BSA_TARGET": "https://bmc.example",
                                  "BSA_ATTEST": "true",
                                  "BSA_ATTEST_LABEL": "lab-rack-3"})
        assert "::warning::" not in r.log
        assert r.argv[-2:] == ["--attest-target-label", "lab-rack-3"]

    def test_a_recorded_walk_does_not_warn(self, tmp_path):
        """There is no target to leak. Warning anyway would train people to ignore
        the warning that matters."""
        r = run_action(tmp_path, {"BSA_WALK": "w.json", "BSA_ATTEST": "true"})
        assert "::warning::" not in r.log


class TestInputsCannotBecomeCommands:
    def test_shell_syntax_in_an_input_arrives_as_one_literal_argument(self, tmp_path):
        """The reason every input goes through `env:`. A workflow expression is
        substituted into the script before bash parses it, so `--config
        "${{ inputs.config }}"` runs whatever the caller wrote."""
        marker = tmp_path / "executed"
        r = run_action(tmp_path, {"BSA_CONFIG": f"a; touch {marker}",
                                  "BSA_WALK": "w.json"})
        assert r.argv == ["detect", "--config", f"a; touch {marker}",
                          "--walk", "w.json"]
        assert not marker.exists(), "the injected command ran"

    def test_no_input_is_interpolated_into_the_audit_script(self):
        """Read out of `action.yml` rather than trusted. The audit step must reach
        its inputs through the environment; a `${{ inputs.* }}` anywhere in that
        script is the defect this class exists for."""
        assert "${{" not in AUDIT_SCRIPT, (
            "the audit script interpolates a workflow expression, which is "
            "substituted before bash parses the line")


class TestTheReadmeDescribesTheActionThatExists:
    """Documentation drift, caught by the suite. The README snippet is what a
    consumer copies; an input renamed in one place and not the other prints a
    workflow that fails on their machine, not ours."""

    @staticmethod
    def _readme_action_inputs() -> set[str]:
        """Input names used under `with:` in the README's action examples."""
        import re
        found: set[str] = set()
        for block in re.findall(r"```yaml\n(.*?)```", README.read_text(), re.S):
            if "james-sheen/bmc-sensor-audit@" not in block and "uses: ./" not in block:
                continue
            for name in re.findall(r"^\s{4}([a-z][a-z0-9-]*):", block, re.M):
                found.add(name)
        return found

    def test_the_readme_shows_the_action_at_all(self):
        assert self._readme_action_inputs(), (
            "the README shows no action example with inputs, so the check below "
            "would pass by finding nothing")

    def test_every_input_the_readme_names_exists_in_the_action(self):
        declared = set(ACTION_SPEC["inputs"])
        printed = self._readme_action_inputs()
        assert printed <= declared, (
            f"the README names {sorted(printed - declared)}, which action.yml does "
            f"not declare")

    def test_every_required_input_is_shown(self):
        required = {name for name, spec in ACTION_SPEC["inputs"].items()
                    if spec.get("required")}
        assert required <= self._readme_action_inputs(), (
            f"the README's example omits required input(s) "
            f"{sorted(required - self._readme_action_inputs())}")

    def test_the_action_declares_the_outputs_the_readme_promises(self):
        readme = README.read_text()
        for output in ACTION_SPEC["outputs"]:
            assert output in readme, f"action.yml declares {output!r}; the README never names it"


class TestTheActionCanInstallTheToolThisRepositoryBuilds:
    """The pin excluded three releases of the tool shipped from this tree.

    `action.yml` said `>=0.1,<0.2` from the day it was written, which was
    correct then. 0.2.0 shipped on 2026-08-26 and the range did not move, so
    `@action-v0` went on resolving 0.1.5 while this repository built 0.2.2 --
    for three releases, at the one surface a stranger adopts us through.

    Four places restate that range and none derived it, so nothing could go
    red. The action canary could not: it watches whether a PERMITTED release
    moved something, and this was a release the range forbade. The question it
    was never asked is the one below -- **can this action install the version
    this repository is currently building?**

    Deliberately the CEILING and not the floor. Asserting the floor tracks the
    newest release would force this pin to move on every tool release and make
    the widening automatic, which is the opposite of what the file argues: the
    range widens when somebody decides a release is compatible. This fails only
    when the tool has moved somewhere the action cannot follow.
    """

    SPECS = re.findall(r"spec='([^']+)'", INSTALL_SCRIPT)

    def test_both_specs_were_found(self):
        """The checks below iterate a regex result, and an empty list iterates
        cleanly. If the case statement is reshaped, this says so."""
        assert len(self.SPECS) == 2, (
            f"expected the detect and coverage specs, found {self.SPECS!r}")

    @pytest.mark.parametrize("raw", SPECS)
    def test_the_spec_admits_the_version_in_this_tree(self, raw):
        req = Requirement(raw)
        assert req.name == "bmc-sensor-audit", raw
        assert req.specifier.contains(__version__), (
            f"action.yml installs {raw!r}, which cannot resolve to "
            f"{__version__} -- the version this repository builds. A consumer "
            f"writing `uses: ...@action-v0` would get an older tool than the "
            f"one released here, and would not be told")

    def test_that_check_could_have_failed(self):
        """The assertion above is only worth having if the range it rejects is
        the range that shipped. This is the literal that was in the file."""
        assert not Requirement("bmc-sensor-audit[detect]>=0.1,<0.2").specifier.contains(
            __version__), (
            f"{__version__} falls inside the range this test exists to reject, "
            f"so the assertion above cannot fail and proves nothing")

    def test_the_modes_differ_only_in_the_engine_extra(self):
        detect, coverage = (Requirement(s) for s in self.SPECS)
        assert detect.extras == {"detect"}, detect.extras
        assert coverage.extras == set(), coverage.extras
        assert detect.specifier == coverage.specifier, (
            "the two modes install different RANGES of the same tool, so a "
            "consumer's tool version depends on which mode they picked")

    def test_the_readme_quotes_the_range_the_action_uses(self):
        """A table cell is a copy, and this is the copy that went stale beside
        the original. It is not derived -- a reader needs to see the range --
        so it is held against the file instead."""
        coverage = next(s for s in self.SPECS if "[" not in s)
        assert coverage in README.read_text(), (
            f"action.yml installs {coverage!r}; the README's tag table names "
            f"something else, and a reader believes the README")
