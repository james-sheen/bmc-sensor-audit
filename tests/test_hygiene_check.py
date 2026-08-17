"""Tests for the pre-commit hygiene check.

**A hygiene check that has never refused anything is not evidence of a clean
tree.** One of the shipped rules was dead on arrival -- the RFC1918 pattern was
missing a separator between its prefix and the remaining octets, so it demanded
four digits where an octet and a dot belonged and never matched an ordinary
private address. It was written, read back, and looked entirely reasonable.
Nothing but planting an address and checking the rule fired could have found it.

So every rule gets two tests: it fires on a plant, and it stays quiet on
something that looks similar and is fine. The second half is the half that keeps
the check usable -- a rule that goes red for a legitimate reason on every run
teaches everyone to skip the whole check, which costs more than the rule is
worth.

`test_every_rule_has_a_plant` is the guard against this file drifting behind the
rule list: add a rule without a plant and it fails, rather than the new rule
quietly never being exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import hygiene_check  # noqa: E402


# One hazard per rule, keyed by rule name. Every string here is invented.
#
# Each line carries the exemption marker, because a file full of plants is a file
# full of matches -- the check finds its own test data otherwise, and the noise
# floor stops being zero. The marker is per line and visible at the site, which
# is the whole reason it is not a per-file exemption.
PLANTS = {
    "github_token": 'T = "github_pat_11AAAA0aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbb"',  # hygiene: synthetic
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----",  # hygiene: synthetic
    "aws_key": 'key = "AKIAIOSFODNN7EXAMPLE"',  # hygiene: synthetic
    "authorization_header": "Authorization: Bearer abcdefghijklmnop",  # hygiene: synthetic
    "private_ip": 'HOST = "10.42.7.19"',  # hygiene: synthetic
    "home_path": 'NOTES = "/home/someone/notes.txt"',  # hygiene: synthetic
    "mac_address": 'MAC = "de:ad:be:ef:00:11"',  # hygiene: synthetic
    "redfish_inventory_field": '{"SerialNumber": "CN7082019L003A"}',  # hygiene: synthetic
}

# Things that resemble a hazard and are not one. Each is present in real trees.
NEAR_MISSES = [
    'BASE = "http://127.0.0.1:8000"',        # the mock BMC binds loopback every run
    'DNS = "8.8.8.8"',                       # public address
    'HOST = "172.15.0.1"',                   # just outside RFC1918
    'HOST = "11.0.0.1"',                     # just outside RFC1918
    'PARTIAL = "de:ad:be"',                  # not a MAC
    '{"SerialNumber": ""}',                  # a field with no value in it
    'path = "/rootkit/thing"',               # not a home directory
    # Upstream entity-manager writes runtime substitutions into inventory fields.
    # The BMC fills these in; the file carries no machine's identity. Vendoring
    # nine upstream configs produced 18 findings of exactly this shape.
    '{"SerialNumber": "$BOARD_SERIAL_NUMBER"}',
    '{"PartNumber": "$PRODUCT_PART_NUMBER"}',
]

# Values that LOOK like the placeholder above and are not. The narrowing that
# admits `$BOARD_SERIAL_NUMBER` must not admit these, or it stops being a rule.
# Every line here fires by design, so every line carries its own marker. Without
# them this file matches its own rule and `test_the_repository_itself_is_clean`
# goes red -- the third time a checker in this repo has found its own test data.
NOT_PLACEHOLDERS = [
    '{"SerialNumber": "CN7082019L003A"}',              # hygiene: synthetic
    '{"SparePartNumber": "05-100051"}',                # hygiene: synthetic
    '{"SerialNumber": "$BOARD_SERIAL_NUMBER extra"}',  # hygiene: synthetic
    '{"SerialNumber": "prefix$BOARD_SERIAL_NUMBER"}',  # hygiene: synthetic
    '{"AssetTag": "Unknown"}',                         # hygiene: synthetic
]


def _scan(tmp_path: Path, text: str):
    (tmp_path / "sample.py").write_text(text + "\n")
    return hygiene_check.scan([Path("sample.py")], tmp_path)


def test_every_rule_has_a_plant():
    """Adding a rule without a hazard to prove it against fails here, rather
    than shipping a rule nothing ever exercises."""
    assert {rule.name for rule in hygiene_check.RULES} == set(PLANTS)


@pytest.mark.parametrize("name", sorted(PLANTS))
def test_each_rule_fires_on_its_hazard(name, tmp_path):
    hits = _scan(tmp_path, PLANTS[name])
    assert [hit[2].name for hit in hits] == [name]


@pytest.mark.parametrize("line", NEAR_MISSES)
def test_near_misses_stay_quiet(line, tmp_path):
    assert _scan(tmp_path, line) == []


@pytest.mark.parametrize("line", NOT_PLACEHOLDERS)
def test_the_template_narrowing_did_not_open_a_hole(line, tmp_path):
    """The paired negative for the placeholder exemption above.

    Narrowing a rule to admit `$BOARD_SERIAL_NUMBER` is only safe while it keeps
    refusing everything else. Without these, the exemption could widen to any
    value containing a template and nothing would notice — an exemption with no
    boundary test is how a rule quietly stops being one.
    """
    hits = _scan(tmp_path, line)
    assert [hit[2].name for hit in hits] == ["redfish_inventory_field"], \
        f"the narrowing let this through: {line}"


def test_the_exemption_marker_silences_a_line(tmp_path):
    """The redaction tests must contain realistic asset tags to assert those tags
    never reach a capture. The check and the test want the same strings for
    opposite reasons."""
    plain = _scan(tmp_path, PLANTS["redfish_inventory_field"])
    marked = _scan(tmp_path, PLANTS["redfish_inventory_field"] + "  # hygiene: synthetic")
    assert len(plain) == 1
    assert marked == []


def test_the_marker_is_per_line_not_per_file(tmp_path):
    """Whole-file exemption would make the exempted file the one place a real
    credential could sit unnoticed."""
    hits = _scan(tmp_path, PLANTS["private_ip"] + "  # hygiene: synthetic\n"
                 + PLANTS["mac_address"])
    assert [hit[2].name for hit in hits] == ["mac_address"]


def test_the_repository_itself_is_clean():
    """The noise floor. This is the assertion that makes the check usable: if it
    is red on this tree for a legitimate reason, everybody learns to skip it."""
    root = Path(__file__).resolve().parents[1]
    paths = [p.relative_to(root) for p in sorted(root.rglob("*")) if p.is_file()]
    hits = hygiene_check.scan(paths, root)
    assert hits == [], "\n".join(f"{h[0]}:{h[1]} [{h[2].name}]" for h in hits)


class TestTheLocalVocabulary:
    """Site-specific rules load from an untracked file rather than shipping here.

    A rule that forbids a private name must spell that name out, so keeping such
    rules in tracked source publishes exactly what they exist to protect. Every
    string below is invented for the same reason — a test proving the mechanism
    must not itself carry the vocabulary.
    """

    VOCAB = {"rules": [{"name": "site_ticket",
                        "pattern": r"(?<![\w-])ZZ-\d{3,}(?![\w-])",
                        "why": "a ticket identifier from a tracker that is not public"}]}

    def _with_vocab(self, tmp_path, vocab):
        (tmp_path / hygiene_check.LOCAL_RULES_FILE).write_text(json.dumps(vocab))
        return hygiene_check.active_rules(tmp_path)

    def test_without_the_file_only_the_shipped_rules_are_active(self, tmp_path):
        assert hygiene_check.active_rules(tmp_path) == hygiene_check.RULES

    def test_a_local_rule_is_loaded_and_fires(self, tmp_path):
        rules = self._with_vocab(tmp_path, self.VOCAB)
        assert "site_ticket" in {r.name for r in rules}
        (tmp_path / "sample.py").write_text("closes ZZ-4171 in the tracker\n")
        hits = hygiene_check.scan([Path("sample.py")], tmp_path, rules=rules)
        assert [h[2].name for h in hits] == ["site_ticket"]

    def test_the_vocabulary_file_is_never_scanned(self, tmp_path):
        """It always matches its own patterns. It is gitignored, so it cannot be
        published — a skip for a file that CANNOT leak, not an exemption for one
        that might."""
        self._with_vocab(tmp_path, self.VOCAB)
        paths = [p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()]
        assert hygiene_check.LOCAL_RULES_FILE in {str(p) for p in paths}
        assert hygiene_check.scan(paths, tmp_path) == []

    def test_an_unusable_vocabulary_refuses_rather_than_running_reduced(self, tmp_path):
        """Silently continuing with fewer rules is the worst outcome: the check
        reports success having stopped looking for half of what it knows."""
        (tmp_path / hygiene_check.LOCAL_RULES_FILE).write_text("{not json")
        with pytest.raises(SystemExit) as exit_info:
            hygiene_check.active_rules(tmp_path)
        assert exit_info.value.code == hygiene_check.EXIT_ERROR

    def test_no_shipped_rule_names_a_private_system(self):
        """The extraction itself, pinned. If a site-specific pattern is ever added
        back to the shipped list, this fails — which is the only thing standing
        between a convenient edit and a public disclosure."""
        for rule in hygiene_check.RULES:
            assert not rule.pattern.search("k8s" + "_orchestrator"), rule.name
            assert not rule.pattern.search("CD" + "-1704"), rule.name


def test_a_match_is_never_printed(capsys, tmp_path, monkeypatch):
    """Echoing a token to a terminal publishes it to a scrollback buffer, a CI
    log, and anything reading either. Only its length is reported."""
    secret = PLANTS["github_token"]
    (tmp_path / "sample.py").write_text(secret + "\n")
    monkeypatch.setattr(hygiene_check, "_tracked_and_untracked",
                        lambda root: [Path("sample.py")])
    code = hygiene_check.main(["--all", "--root", str(tmp_path)])
    assert code == hygiene_check.EXIT_FOUND
    captured = capsys.readouterr()
    assert "github_pat_" not in captured.err + captured.out
    assert "characters on this line" in captured.err
