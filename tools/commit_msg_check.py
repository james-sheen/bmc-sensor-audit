#!/usr/bin/env python3
"""Check a commit message before it becomes permanent.

**A commit message is the one published surface that can never be corrected.** A
file can be fixed in the next commit; a README can be rewritten; a released
package can be superseded. A message is immutable the moment it is pushed, and no
check can ever read it afterwards to tell you it was wrong.

That asymmetry is the whole reason this exists. The hygiene check guards files and
cannot see the message. This guards the message using the same rules, so a token,
an internal ticket id or a private module path cannot reach history through the
one door that has no lock on it.

    git config core.hooksPath .githooks     # once, per clone -- enables this too
    python3 tools/commit_msg_check.py FILE  # check a message by hand

Exit 0 clean, 1 refused, 2 the check could not run.

WHAT THIS DELIBERATELY DOES NOT CHECK: imperative mood. Git's convention is that a
subject completes *this commit will...*, because git composes sentences around it
-- `git revert` on a declarative subject reads as reverting TO the broken state.
The convention is real and worth following. It is not checkable: the clearest
violation this project has produced began with the word `declare`, which any
first-word heuristic reads as a perfect imperative. A rule that cannot separate
the good case from the bad one would refuse honest messages, and a hook that
refuses honest work is a hook people learn to pass `--no-verify` -- which turns
off the leak rules too. So the mood convention is documented and not enforced.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hygiene_check  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_CLEAN, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2

SUBJECT_MAX = 72

# A number that looks derived rather than incidental. Years are excluded because a
# copyright line is the common false positive and it is not a measurement.
NUMBER = re.compile(r"(?<![\w.@/-])(\d{1,3}(?:,\d{3})+|\d{3,})(?![\w.:/-])")
YEAR = re.compile(r"^(19|20)\d\d$")
# Something a reader could re-derive the number from.
BASIS = re.compile(r"@[0-9a-f]{7,}|\b(19|20)\d\d-\d\d-\d\d\b|\bv\d+\.\d+")


def _message_lines(text: str) -> list[str]:
    """The message as git will store it: comment lines are stripped."""
    return [line for line in text.splitlines() if not line.startswith("#")]


def check(text: str) -> tuple[list[str], list[str]]:
    """Return (refusals, warnings)."""
    refusals: list[str] = []
    warnings: list[str] = []
    lines = _message_lines(text)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[0].strip():
        return (["the message is empty"], [])

    subject = lines[0]

    if len(subject) > SUBJECT_MAX:
        refusals.append(
            f"the subject is {len(subject)} characters; keep it to {SUBJECT_MAX} "
            "so it survives --oneline, blame and release notes uncut")
    if subject.rstrip().endswith("."):
        refusals.append("the subject ends with a full stop; it is a title, not a sentence")
    if len(lines) > 1 and lines[1].strip():
        refusals.append(
            "no blank line after the subject, so every tool that splits on the "
            "first blank line will treat the whole message as one subject")

    # The leak rules, applied to the surface they cannot otherwise reach.
    for number, line in enumerate(lines, start=1):
        if hygiene_check.EXEMPT.search(line):
            continue
        for rule in hygiene_check.active_rules(REPO_ROOT):
            found = rule.pattern.search(line)
            if found:
                refusals.append(
                    f"line {number} carries {rule.why.split('.')[0]} "
                    f"({len(found.group(0))} characters; not shown)")

    for number, line in enumerate(lines, start=1):
        if BASIS.search(line):
            continue
        for match in NUMBER.finditer(line):
            if YEAR.match(match.group(0)):
                continue
            warnings.append(
                f"line {number}: {match.group(0)} has no measurement basis. A "
                "derived count in a message can never be corrected or re-derived "
                "-- add the source, as in (openbmc/entity-manager@0ada0483)")
    return refusals, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("message", help="path to the commit message file")
    args = parser.parse_args(argv)

    path = Path(args.message)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        print(f"commit-msg: cannot read {path}: {error}", file=sys.stderr)
        return EXIT_ERROR

    refusals, warnings = check(text)

    for warning in warnings:
        print(f"commit-msg: note -- {warning}", file=sys.stderr)

    if not refusals:
        if warnings:
            print(f"commit-msg: {len(warnings)} note(s); message accepted",
                  file=sys.stderr)
        return EXIT_CLEAN

    print(f"\ncommit-msg: {len(refusals)} problem(s) -- commit refused\n",
          file=sys.stderr)
    for refusal in refusals:
        print(f"  {refusal}", file=sys.stderr)
    print("\n  The message is in your editor's file and was not discarded.",
          file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
