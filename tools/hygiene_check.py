#!/usr/bin/env python3
"""Refuse to commit things that should not be published.

This repository is **authored in public**. There is no private tree, no scrub and
no staging step between what gets written here and what the world reads. That
removes an entire class of bug -- nothing can be mangled in translation -- and it
removes the safety net at the same time. There is nowhere for a check to live
except before the commit, which is where this runs.

    git config core.hooksPath .githooks      # once, per clone
    python3 tools/hygiene_check.py --all     # sweep the whole tree
    python3 tools/hygiene_check.py           # staged content only (the hook)

Exit 0 clean, 1 something was found, 2 the check could not run. The third is
distinct because a hygiene check that cannot run must not read as a pass.

WHAT THIS CANNOT DO. It matches patterns, so it finds the shapes it knows and
nothing else. A pattern list written by asking *what might I leak?* enumerates
what its author already remembers not to do; that is why the last class below
came from asking what this project handles that no other one does, rather than
from a general checklist. Treat a clean run as the absence of known shapes, not
as evidence the diff is safe to publish.

THE EXEMPTION MARKER. A line ending in `hygiene: synthetic` is skipped. It exists
because the redaction tests must contain realistic-looking asset tags in order to
assert that they never reach a capture -- the check and the test want the same
strings for opposite reasons. The cost is real and worth stating: a genuine
secret pasted onto a marked line is invisible here. The marker is per-line, never
per-file, so it stays visible at the site and in review.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

EXIT_CLEAN, EXIT_FOUND, EXIT_ERROR = 0, 1, 2

EXEMPT = re.compile(r"hygiene:\s*synthetic")

# Binary and generated content: scanning it produces noise, not findings.
SKIP_SUFFIXES = {".png", ".jpg", ".gif", ".ico", ".pdf", ".whl", ".gz", ".zip"}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "build", "dist", ".venv"}


class Rule:
    def __init__(self, name: str, pattern: str, why: str, flags: int = 0) -> None:
        self.name, self.why = name, why
        self.pattern = re.compile(pattern, flags)


RULES = [
    # 1. Credentials. The one class with no acceptable exception.
    Rule("github_token", r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}",
         "a GitHub token. Rotate it, then remove it -- a token in a commit is "
         "published the moment the commit is, and rewriting history does not "
         "reach anyone who already cloned"),
    Rule("private_key", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
         "a private key"),
    Rule("aws_key", r"AKIA[0-9A-Z]{16}", "an AWS access key id"),
    Rule("authorization_header", r"[Aa]uthorization[\"']?\s*[:=]\s*[\"']?(?:Basic|Bearer)\s+\S{8,}",
         "a hardcoded Authorization header with a real-looking credential"),

    # 2. Real infrastructure. Loopback is deliberately absent: the mock BMC binds
    #    127.0.0.1 on every test run, and a rule that fires on that fires always.
    #    The first cut of this rule was DEAD. It read
    #      (?:10\.\d{1,3}|192\.168|...)\d{1,3}\.\d{1,3}
    #    with no separator between the prefix group and the remaining octets, so
    #    it required 10.4277.19 and never matched 10.42.7.19. It was written,
    #    read back, and looked entirely reasonable. Only planting an address and
    #    checking the rule fired showed it matched nothing at all -- which is why
    #    every rule here has a test that plants its hazard, and why a hygiene
    #    check that has never refused anything is not evidence of a clean tree.
    Rule("private_ip",
         r"(?<![\w.])(?:10(?:\.\d{1,3}){3}"
         r"|192\.168(?:\.\d{1,3}){2}"
         r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\w.])",
         "an RFC1918 address, which names a real internal network"),
    Rule("home_path", r"/(?:home|Users)/[a-z][\w.-]*|/root/[\w.-]+",
         "a path inside somebody's home directory"),

    # 3. Anything naming a system that is not public is SITE-SPECIFIC and lives
    #    in the local vocabulary file, never here. See LOCAL_RULES_FILE below:
    #    a rule that must spell out a private name in order to forbid it would
    #    publish that name to everyone who reads this file, which is the exact
    #    disclosure the rule exists to prevent.

    # 4. Hardware identity. THE CLASS THIS PROJECT OWNS, and the reason the file
    #    exists. A Redfish walk of a real machine returns serial numbers, part
    #    numbers, asset tags and MAC addresses, and the natural way to build a
    #    realistic fixture is to capture one and commit it. `capture` writes only
    #    the parsed sensor set for exactly this reason; this catches the paths
    #    that go around it.
    Rule("mac_address", r"(?<![\w:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![\w:])",
         "a MAC address, which identifies one physical machine"),
    # The value must not be a bare `$TEMPLATE` reference. Upstream entity-manager
    # configurations write `"SerialNumber": "$BOARD_SERIAL_NUMBER"` -- a
    # substitution the BMC fills in at runtime, which by construction carries no
    # machine's identity. Vendoring nine of them produced 18 findings of exactly
    # that shape, and the alternatives were both worse than narrowing the rule:
    # the files are third-party and must stay verbatim, so a per-line marker
    # cannot be added, and a per-directory exemption would create precisely the
    # unwatched corner this checker's own design forbids.
    #
    # The narrowing is deliberately the smallest one that works. A value that is
    # ENTIRELY one `$identifier` is a placeholder; anything else -- including
    # `Unknown` and a real part number -- still fires.
    Rule("redfish_inventory_field",
         r"\"(?:SerialNumber|PartNumber|AssetTag|SKU|SparePartNumber|UUID)\""
         r"\s*:\s*\"(?!\$[A-Za-z_]\w*\")[^\"]+\"",
         "a Redfish inventory field with a value. A committed chassis walk is a "
         "fleet inventory disclosure; capture the parsed sensor set instead"),
]


# Site-specific vocabulary lives here, untracked. A rule that forbids a private
# name has to spell that name out, so keeping such rules in this file would
# publish exactly what they exist to protect -- and this file is public.
#
# Ship generic; each clone supplies its own. Format:
#
#     {"rules": [{"name": "internal_ticket",
#                 "pattern": "(?<![\\w-])CD-\\d{2,}(?![\\w-])",
#                 "why": "an internal ticket identifier"}]}
LOCAL_RULES_FILE = ".hygiene-local.json"


def load_local_rules(root: Path) -> list[Rule]:
    """Extra rules from the untracked local vocabulary, or none.

    A missing file is normal and not an error -- but it is REPORTED rather than
    silent, because losing coverage without being told is how a check becomes
    decorative. That is the same failure as a hook nobody enabled.
    """
    path = root / LOCAL_RULES_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Rule(entry["name"], entry["pattern"], entry["why"])
                for entry in data["rules"]]
    except (ValueError, KeyError, TypeError, re.error) as error:
        print(f"hygiene: {path} is unusable ({error}); site rules are NOT active",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def active_rules(root: Path) -> list[Rule]:
    """Everything this clone checks: the shipped generic set plus local."""
    return RULES + load_local_rules(root)


def _is_scannable(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    # The vocabulary file always matches its own patterns -- a rule that forbids
    # a string has to contain that string. It is gitignored, so it is
    # structurally incapable of being published, which makes scanning it pure
    # noise. This is a skip for a file that CANNOT leak, not an exemption for one
    # that might: the difference is why it is safe.
    if path.name == LOCAL_RULES_FILE:
        return False
    return not (SKIP_PARTS & set(path.parts))


def _staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(f"hygiene: cannot list staged files: {result.stderr.strip()}",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return [Path(line) for line in result.stdout.splitlines() if line]


def _tracked_and_untracked(root: Path) -> list[Path]:
    return [p.relative_to(root) for p in sorted(root.rglob("*")) if p.is_file()]


def scan(paths: list[Path], root: Path,
         rules: list[Rule] | None = None) -> list[tuple[Path, int, Rule, str]]:
    if rules is None:
        rules = active_rules(root)
    hits: list[tuple[Path, int, Rule, str]] = []
    for relative in paths:
        path = root / relative
        if not path.is_file() or not _is_scannable(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if EXEMPT.search(line):
                continue
            for rule in rules:
                found = rule.pattern.search(line)
                if found:
                    hits.append((relative, number, rule, found.group(0)))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="scan the whole tree rather than staged content")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    paths = _tracked_and_untracked(root) if args.all else _staged_files()
    if not paths:
        print("hygiene: nothing to scan")
        return EXIT_CLEAN

    local = load_local_rules(root)
    if local:
        print(f"hygiene: {len(RULES)} shipped rule(s) + {len(local)} from "
              f"{LOCAL_RULES_FILE}")
    else:
        print(f"hygiene: {len(RULES)} shipped rule(s); no {LOCAL_RULES_FILE}, so "
              "no site-specific vocabulary is being checked")

    hits = scan(paths, root, rules=RULES + local)
    if not hits:
        print(f"hygiene: {len(paths)} file(s) scanned, nothing found")
        return EXIT_CLEAN

    print(f"hygiene: {len(hits)} finding(s) -- commit refused\n", file=sys.stderr)
    for path, number, rule, matched in hits:
        # The match itself is NOT printed. Echoing a token to a terminal
        # publishes it to a scrollback buffer, a CI log and anything reading
        # either. The length is enough to find it by.
        print(f"  {path}:{number}  [{rule.name}] {rule.why}", file=sys.stderr)
        print(f"      matched {len(matched)} characters on this line; not shown",
              file=sys.stderr)
    print("\n  If a match is deliberately fake, end that line with a comment "
          "reading  hygiene: synthetic", file=sys.stderr)
    return EXIT_FOUND


if __name__ == "__main__":
    raise SystemExit(main())
