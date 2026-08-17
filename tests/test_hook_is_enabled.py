"""Make an unenabled hook LOUD instead of silent.

The pre-commit hygiene check cannot enable itself. Git refuses to let a cloned
repository set its own `core.hooksPath`, and that refusal is right — a repo that
could would be arbitrary code execution on clone. So activation is a manual step
per clone, and a manual step is a step that gets skipped.

It was skipped here. `core.hooksPath` was unset in the authoring clone for the
first three commits, so this repository's only gate had never run on any of them.
Nothing said so, because the failure mode of a hook that is not installed is
silence — commits simply succeed, exactly as they do when the check passes.

**This test cannot install the hook** (a test suite that reconfigures your git
checkout is a worse problem than the one it solves). What it can do is refuse to
be quiet, so anyone running the suite finds out in one line instead of finding out
never. CI is the actual backstop; this is the fast local signal.

The cost, stated rather than hidden: it is skipped in CI, where hooks are
meaningless and the workflow runs the sweep directly. A test skipped in the place
it would otherwise be loudest is a real gap, which is why the workflow does not
depend on it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ".githooks"


def _hooks_path() -> str | None:
    result = subprocess.run(["git", "config", "core.hooksPath"],
                            cwd=str(ROOT), capture_output=True, text=True)
    return result.stdout.strip() or None


class TestTheHookIsInstalled:
    @pytest.mark.skipif(os.environ.get("CI") == "true",
                        reason="hooks are meaningless on a CI runner; the workflow "
                               "runs the hygiene sweep directly instead")
    def test_core_hookspath_points_at_the_versioned_hooks(self):
        configured = _hooks_path()
        assert configured == EXPECTED, (
            f"core.hooksPath is {configured!r}, expected {EXPECTED!r}.\n"
            "The pre-commit hygiene check is NOT running in this clone, and a\n"
            "commit that should be refused will succeed silently.\n"
            "\n"
            "    git config core.hooksPath .githooks\n"
            "\n"
            "Git will not let the repository do this for you, by design.")


class TestTheHookItself:
    """Independent of whether it is installed — if the hook is broken, enabling it
    achieves nothing."""

    def test_the_hook_exists_and_is_executable(self):
        hook = ROOT / EXPECTED / "pre-commit"
        assert hook.is_file(), f"no hook at {hook}"
        assert os.access(hook, os.X_OK), (
            f"{hook} is not executable; git will skip it without a word")

    def test_the_hook_invokes_the_checker_the_repo_ships(self):
        """A hook calling a checker that moved is a hook that fails open."""
        body = (ROOT / EXPECTED / "pre-commit").read_text()
        assert "tools/hygiene_check.py" in body
        assert (ROOT / "tools" / "hygiene_check.py").is_file()

    def test_the_activation_command_is_identical_everywhere_it_appears(self):
        """It appears twice in the README on purpose — once under Try it, where a
        reader meets the failure, and once under Hygiene, where the reasoning is.
        Two copies of a command is two chances to drift, so pin them equal rather
        than trusting nobody edits one."""
        readme = (ROOT / "README.md").read_text()
        occurrences = re.findall(r"git config core\.hooksPath \S+", readme)
        assert len(occurrences) >= 2, \
            f"expected the activation command in both sections, found {occurrences}"
        assert len(set(occurrences)) == 1, \
            f"the README gives conflicting activation commands: {set(occurrences)}"
        assert occurrences[0].endswith(EXPECTED), \
            f"the README activates a different hooks path: {occurrences[0]}"

    def test_the_hook_names_its_own_activation_step(self):
        """The one line a reader needs when this test fails must live in the hook
        too, because that is where someone looks after reading the failure."""
        assert "core.hooksPath" in (ROOT / EXPECTED / "pre-commit").read_text()


class TestTheServerSideBackstop:
    """The layer that no local configuration can switch off."""

    def test_a_workflow_runs_the_hygiene_sweep(self):
        workflow = ROOT / ".github" / "workflows" / "checks.yml"
        assert workflow.is_file(), "no CI workflow; the hook is then the only gate"
        body = workflow.read_text()
        assert "hygiene_check.py --all" in body, \
            "the workflow does not run the hygiene sweep"

    def test_the_workflow_also_backstops_the_commit_message_hook(self):
        """Both hooks hang off one `core.hooksPath`, which was unset here for
        three commits. Backstopping only the file sweep would leave the message
        check with exactly the weakness that made the sweep's backstop necessary
        — and a message is the surface no later commit can correct."""
        body = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
        assert "commit_msg_check.py" in body, \
            "CI does not check commit messages; the commit-msg hook has no backstop"

    def test_the_workflow_fetches_enough_history_to_do_that(self):
        """`actions/checkout` defaults to depth 1, which would silently reduce the
        message check to the tip commit — passing while checking almost nothing."""
        body = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
        assert "fetch-depth: 0" in body, \
            "shallow checkout: the commit-message step can only see HEAD"

    def test_the_workflow_runs_on_push_not_only_on_a_schedule(self):
        """A sibling project's scheduled-only workflow reached zero runs across
        fifteen pushes. On push is what makes a check real."""
        body = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
        trigger_block = body.split("jobs:", 1)[0]
        assert "push:" in trigger_block, \
            "the workflow does not trigger on push; it will not run when it matters"
