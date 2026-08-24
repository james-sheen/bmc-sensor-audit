"""The hygiene vocabulary reaches a commit message, and the nickname rule.

**The defect this file exists for.** The hygiene rules describe what must never be
published. They had only ever been run over FILES, so every rule in the set was
silently narrower than the sentence it enforces — and a commit message is the one
published surface that cannot be corrected after a push.

An internal repository nickname reached the commit messages of four public
repositories at once. A rule matching it already existed in a private scrub and
would have caught it; the guard that was actually ASKED about a commit message had
never heard of it. **The rule was right and the surface was missing**, which is a
failure no amount of care about rules would have found.

**The `commit-msg` hook claimed otherwise, in a comment.** It said *the hygiene
check guards files and cannot see the message; this runs the same rules over it* —
and it ran only the message-shape checker, which knows about subject length and
number bases and nothing about publication. The comment described the property that
would have prevented the leak. Nothing checked the claim, because it was prose.
`TestTheHookRunsWhatItSaysItRuns` is that check now.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "hygiene_check.py"
HOOK = ROOT / ".githooks" / "commit-msg"

#: The literal that reached four public repositories. Held here so the non-vacuity
#: check below is against the real thing rather than a paraphrase of it.
THE_LEAK = ("- The two checks are now byte-identical across all four. Repo #1 "  # hygiene: synthetic
            "tracks every\n  tree, so the check that they stay that way lives there.\n")


def hygiene():
    spec = importlib.util.spec_from_file_location("hygiene_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *argv],
                          capture_output=True, text=True, cwd=str(ROOT))


class TestTheNicknameRuleShips:
    def test_it_is_in_the_shipped_set_not_the_local_one(self):
        """Generic, so it ships. The pattern spells out a SHAPE rather than a
        name, so publishing it discloses nothing -- which is the test every rule
        in the shipped set has to pass."""
        names = {rule.name for rule in hygiene().RULES}
        assert "repository_nickname" in names

    def test_it_catches_the_literal_that_shipped(self):
        """Non-vacuity against the real message, not a paraphrase of it."""
        module = hygiene()
        hits = module.scan_text(THE_LEAK, "(message)", module.RULES)
        assert [h[2].name for h in hits] == ["repository_nickname"]

    @pytest.mark.parametrize("text,expected", [
        ("repository #3 holds it", True),  # hygiene: synthetic
        ("Repo #12", True),  # hygiene: synthetic
        ("repo  #4", True),  # hygiene: synthetic
        ("repo#7", True),  # hygiene: synthetic
        ("the repository holds it", False),
        ("reported a defect", False),
        ("https://github.com/x/repo#readme", False),
        ("repo number 3", False),
    ])
    def test_the_spellings_around_it(self, text, expected):
        """`repo` appears inside `repository` and `reported`, and as a URL
        fragment. A word-level match would flag all three."""
        module = hygiene()
        hits = module.scan_text(text, "(message)", module.RULES)
        assert bool(hits) is expected

    def test_the_reason_says_it_cannot_be_substituted(self):
        """The `why` is what a person reads at the moment they are refused. If it
        suggested a replacement somebody would apply one, and a nickname has no
        correct replacement -- only a rewrite the author has to do."""
        why = next(r.why for r in hygiene().RULES if r.name == "repository_nickname")
        assert "no substitution" in why or "there is no substitution" in why


class TestTheMessageSurfaceExists:
    def test_a_clean_message_passes_and_says_what_it_checked(self, tmp_path):
        path = tmp_path / "msg.txt"
        path.write_text("Anchor the rule on the version literal\n")
        result = run("--message", str(path))
        assert result.returncode == 0, result.stderr
        assert "rule(s), nothing found" in result.stdout

    def test_the_message_that_shipped_is_refused(self, tmp_path):
        path = tmp_path / "msg.txt"
        path.write_text(THE_LEAK)
        result = run("--message", str(path))
        assert result.returncode == 1
        assert "repository_nickname" in result.stderr

    def test_an_unreadable_message_exits_2_not_1(self, tmp_path):
        """Could-not-check is not found-nothing, and it is not found-something
        either. The same three-valued contract the audit tool uses."""
        result = run("--message", str(tmp_path / "absent.txt"))
        assert result.returncode == 2

    def test_every_shipped_rule_reaches_the_message(self, tmp_path):
        """The point of the change: the vocabulary is one set, run over two
        surfaces. A rule that only ever sees files is narrower than the sentence
        it enforces, and nothing would say so."""
        module = hygiene()
        for rule in module.RULES:
            assert rule in module.RULES
        text = "Repo #1 and 10.1.2.3 on one line\n"  # hygiene: synthetic
        hits = module.scan_text(text, "(message)", module.RULES)
        found = {h[2].name for h in hits}
        assert {"repository_nickname", "private_ip"} <= found, (
            f"the message surface reached only {found}; it must run the whole "
            f"shipped vocabulary, not a subset chosen for messages")

    def test_the_synthetic_marker_works_on_a_message_too(self, tmp_path):
        """The escape hatch has to behave the same on both surfaces, or somebody
        meets a refusal they cannot clear and reaches for --no-verify."""
        module = hygiene()
        text = "Repo #1 in an example  hygiene: synthetic\n"
        assert module.scan_text(text, "(message)", module.RULES) == []


class TestTheHookRunsWhatItSaysItRuns:
    """The hook's comment claimed it ran the hygiene rules and it did not.

    A claim in a comment is the cheapest thing to get wrong and the last thing
    anybody re-reads. This compares the two.
    """

    def test_the_hook_invokes_both_checkers(self):
        body = HOOK.read_text()
        assert "hygiene_check.py" in body and "--message" in body, (
            "the commit-msg hook does not run the hygiene rules over the message; "
            "its comment has said it does since before that was true")
        assert "commit_msg_check.py" in body

    @staticmethod
    def _commands() -> list[str]:
        """The lines the shell will RUN, not the ones explaining them.

        The first version of this compared positions in the whole file and read
        the comment that names both checkers -- so it asserted the order of a
        sentence about the hook rather than the order of the hook. Reading the
        wrong surface is the defect this whole file exists for, reproduced in the
        check written to police it.
        """
        return [line for line in HOOK.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    def test_publication_is_checked_before_shape(self):
        """Order is not cosmetic: one of these is about a surface that cannot be
        corrected after a push, and the other is about house style."""
        commands = "\n".join(self._commands())
        assert "hygiene_check.py" in commands, "the hook does not RUN the hygiene rules"
        assert commands.index("hygiene_check.py") < commands.index("commit_msg_check.py")

    def test_the_hook_stops_on_the_first_failure(self):
        """Without `set -e` the first checker's refusal is discarded by the
        second's success, which is how a gate reports the wrong answer."""
        assert re.search(r"^set -e", HOOK.read_text(), re.MULTILINE)

    def test_the_hook_refuses_the_message_that_shipped(self, tmp_path):
        path = tmp_path / "msg.txt"
        path.write_text(THE_LEAK)
        result = subprocess.run(["sh", str(HOOK), str(path)],
                                capture_output=True, text=True, cwd=str(ROOT))
        assert result.returncode != 0
        assert "repository_nickname" in result.stderr

    def test_the_hook_passes_a_clean_message(self, tmp_path):
        """Non-vacuity: a hook that refused everything would pass the test above
        and stop anyone committing."""
        path = tmp_path / "msg.txt"
        path.write_text("Anchor the release-tag rule on the version literal\n")
        result = subprocess.run(["sh", str(HOOK), str(path)],
                                capture_output=True, text=True, cwd=str(ROOT))
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
