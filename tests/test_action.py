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
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="action.yml is YAML; CI installs PyYAML")

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"
README = ROOT / "README.md"

ACTION_SPEC = yaml.safe_load(ACTION.read_text())
AUDIT_SCRIPT = next(s for s in ACTION_SPEC["runs"]["steps"]
                    if s.get("id") == "audit")["run"]

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
