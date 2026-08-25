"""Every documented parser finding, reproduced from files in this repository.

Acceptance criterion 1 was proven by a run nobody else could make: the parser
claims were measured against upstream configurations on a local checkout, by path
and at no identifiable revision. True, and unverifiable by any reader — which for
a claim in a public README is the same as unproven.

The fixtures are now pinned to `0ada048303bb`. The first attempt was not, and within a
day two of the nine had been renamed upstream and every corpus count had moved.

`tests/fixtures/upstream/` now carries thirteen of those files verbatim, chosen by
measuring the corpus for each documented property and taking the smallest file
exhibiting it that also passes this project's hygiene check. The three
`meta/bletchley/` files are there for a different reason: with the captured walk
beside them they make a real coverage diff reproducible from a clone. This module runs the
shipped reader over them and asserts each finding actually appears.

**These tests fail if the fixtures are edited.** That is the point — a fixture
adjusted until a test passes proves only that the test passes. The files are
third-party content and their whole value is being exactly what upstream ships.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "upstream"
sys.path.insert(0, str(ROOT / "src"))

# The two key sets are imported rather than restated, so this oracle cannot drift
# from the reader it checks — a vocabulary written down twice will diverge. The
# ALGORITHM below stays a deliberately separate implementation; only the
# vocabulary is shared.
from bmc_sensor_audit.inventory.entity_manager import (  # noqa: E402
    ANY_TEMPLATE, load_declaration,
    _CHANNEL_SUFFIXES as CHANNEL_SUFFIXES,
    _NOT_A_CHANNEL as NOT_A_CHANNEL)


@pytest.fixture(scope="module")
def corpus():
    return load_declaration([str(UPSTREAM)])


class TestTheFixturesArePresentAndLicensed:
    def test_thirteen_configurations_are_vendored(self):
        assert len(list(UPSTREAM.rglob("*.json"))) == 13

    def test_the_upstream_licence_travels_with_them(self):
        """Apache-2.0 requires the notice to accompany redistribution. Upstream
        spells the file LICENCE; a LICENSE glob misses it, which is how this was
        nearly shipped without one."""
        licence = UPSTREAM / "LICENCE"
        assert licence.is_file(), "upstream licence not redistributed alongside"
        body = licence.read_text()
        assert "Apache License" in body
        assert "Copyright 2018 Intel Corporation" in body

    def test_the_notice_pins_an_upstream_revision(self):
        """The pin is the whole remedy. Without it these are a snapshot of an
        unidentifiable revision — which is what the first version of this
        directory was, and within a day two of its nine files had been renamed
        upstream with no way for a reader to tell."""
        notice = (ROOT / "NOTICE").read_text()
        sha = re.search(r"\b[0-9a-f]{40}\b", notice)
        assert sha, "NOTICE no longer pins a 40-character upstream commit"
        readme = (UPSTREAM / "README.md").read_text()
        assert sha.group(0) in readme, \
            "NOTICE and the fixture README disagree about the pinned revision"

    def test_the_notice_declares_the_redistribution(self):
        """A NOTICE saying nothing upstream is redistributed, beside a directory
        of redistributed upstream files, is the defect this repository keeps
        finding elsewhere."""
        notice = (ROOT / "NOTICE").read_text()
        assert "Intel Corporation" in notice
        assert "tests/fixtures/upstream/" in notice
        assert "No configuration file from any upstream repository is" not in notice


class TestTheDocumentedFindingsReproduce:
    def test_every_file_is_read_and_none_is_unreadable(self, corpus):
        assert corpus.files_read == 13
        assert corpus.unreadable == [], corpus.unreadable
        assert len(corpus.sensors) > 0

    def test_block_comments_are_read_not_skipped(self, corpus):
        """Ten of the 349 are not strict JSON. A tool that skips them reports
        their sensors as undeclared rather than unread — a false clean bill of
        health for the whole board."""
        path = UPSTREAM / "meta" / "catalina" / "catalina_osfp.json"
        with pytest.raises(ValueError):
            json.loads(path.read_text())
        assert any(s.source.endswith("catalina_osfp.json") for s in corpus.sensors), \
            "the JSONC-tolerant reader did not recover this file"

    def test_a_top_level_list_is_handled(self, corpus):
        path = UPSTREAM / "intel" / "axx1p100hssi_aic.json"
        assert isinstance(json.loads(path.read_text()), list)
        assert any(s.source.endswith("axx1p100hssi_aic.json") for s in corpus.sensors)

    def test_one_entry_can_declare_several_sensors(self, corpus):
        """Counting `Exposes` entries counts boards, not sensors.

        The expansion is driven by per-THRESHOLD `Label` fields, not by an
        entry's `Labels` array — which is worth stating because the obvious
        reading is the other way round, and picking a fixture on that reading
        selected a file that demonstrates nothing.
        """
        hsc = [s for s in corpus.sensors
               if s.source.endswith("fbyv2.json") and s.name == "HSC" and s.label]
        assert len(hsc) > 1, "the multi-sensor entry no longer expands"
        assert len({s.label for s in hsc}) == len(hsc), \
            "rails collapsed together; a labelled entry is several sensors"

    def test_a_rail_declared_only_by_labels_is_declared(self, corpus):
        """The lead that was closed too early, reopened and answered properly.

        The report read: *this parser never consults an entry's `Labels` array,
        and a sensor declared through `Labels` alone would be invisible to it*.
        The previous version of this test agreed with the second half and pinned
        it -- *the original open question survives untouched* -- which converted a
        correct outside finding into documented behaviour.

        It was a false clean, in the one direction that matters. Nothing expected
        those rails, so their absence could never be reported.
        """
        rails = [s for s in corpus.sensors if s.source.endswith("mtjade.json")
                 and s.display_name in ("PSU0_PINPUT", "PSU0_POUTPUT")]
        assert len(rails) == 2, (
            "the Mt.Jade PSU declares pin and pout1 in its `Labels` array with a "
            "threshold on neither; both should be declared")
        assert all(not s.thresholds for s in rails), (
            "these two are the specimen precisely because nothing bounds them; if "
            "they grew thresholds the test stops covering the case")

    def test_the_labels_and_channels_overlap_is_still_reported(self, corpus):
        """The other half of the rule, unchanged. An entry carrying both a
        `Labels` array and several `Name<n>` channels is ambiguous -- the list can
        select which channels exist at all -- so only the primary is counted and
        the ambiguity is reported rather than resolved by guess."""
        overlapping = [a for a in corpus.anomalies
                       if a.kind == "ambiguous_channel_naming"]
        assert overlapping, \
            "no vendored entry exercises the Labels-plus-channels overlap any " \
            "more; the fixture set no longer covers the case it is pinned for"
        assert all(a.source.endswith("cx7_mezzanine_module.json")
                   for a in overlapping)

    def test_every_declared_channel_is_derived_not_transcribed(self, corpus):
        """The pin that would have caught the original defect.

        It asserts a PROPERTY, not a number: the set of names the reader produces
        equals the set a second, independent reading of the raw JSON says it
        should. No count is written down here, so the test keeps working when the
        corpus pin moves — and a count is exactly what the old tests pinned while
        the reader was quietly dropping channels.

        Set equality in both directions on purpose. Containment would pass while
        the reader invented a name, and this defect's own symptom was a name
        appearing on one side and not the other.
        """
        expected: set[str] = set()
        for path in sorted(UPSTREAM.rglob("*.json")):
            text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
            document = json.loads(text)
            for record in (document if isinstance(document, list) else [document]):
                if not isinstance(record, dict):
                    continue
                for entry in record.get("Exposes") or []:
                    if not isinstance(entry, dict):
                        continue
                    channels = [
                        value for key, value in entry.items()
                        if isinstance(value, str) and value
                        and key not in NOT_A_CHANNEL
                        and (key == "Name" or re.fullmatch(r"Name\d+", key)
                             or key[4:].lower() in CHANNEL_SUFFIXES)]
                    if not channels:
                        continue
                    thresholds = [t for t in entry.get("Thresholds") or []
                                  if isinstance(t, dict)]
                    # The rail set, derived the way the reader now derives it: from
                    # the `Labels` array that DECLARES it, plus any rail a threshold
                    # names that the array omits.
                    rails = [str(x) for x in (entry.get("Labels") or [])
                             if isinstance(x, (str, int))]
                    rails += [str(t["Label"]) for t in thresholds
                              if t.get("Label") and str(t["Label"]) not in rails]
                    if rails and len(channels) > 1:
                        # Rails and channels overlapping is ambiguous; the entry
                        # contributes its first name only.
                        expected.add(channels[0])
                    elif rails:
                        # One sensor per rail. A rail carrying `<label>_Name` takes
                        # that name; the rest keep the entry's name and are told
                        # apart by their label.
                        for rail in rails:
                            override = entry.get(f"{rail}_Name")
                            expected.add(str(override)
                                         if isinstance(override, str) and override
                                         else channels[0])
                    else:
                        expected.update(channels)

        assert {s.name for s in corpus.sensors} == expected

    @pytest.mark.parametrize("token,filename", [
        ("$index", "8x25_hsbp.json"),
        ("$ipmbindex", "twinlake.json"),
        ("$bus", "cx7_mezzanine_module.json"),
    ])
    def test_each_runtime_template_variable_is_recognised(self, corpus, token, filename):
        named = [s for s in corpus.sensors if s.source.endswith(filename)]
        assert named, f"{filename} declared nothing"
        templated = [s for s in named if ANY_TEMPLATE.search(s.name)]
        assert templated, f"no templated name found in {filename}"

    def test_a_compound_template_is_not_swallowed_whole(self, corpus):
        """`$bus_ADC0` broke the first matcher: `_` is a word character, so a
        greedy variable pattern ate the whole token and degenerated into a
        match-anything expression."""
        names = [s.name for s in corpus.sensors
                 if s.source.endswith("cx7_mezzanine_module.json")]
        compound = [n for n in names if "$bus_" in n]
        assert compound, "the compound-template fixture no longer exercises the case"

    def test_disabled_entries_are_declared_but_marked(self, corpus):
        """Present-but-disabled is the middle value of a three-valued presence,
        and the case the tool exists for. It must not be dropped at read time.

        This asserted for a while that every disabled entry came from
        `spc621d8hm3.json`, which was true when that was the only fixture
        carrying one and stopped being true the moment the bletchley files were
        vendored. Exclusivity was never the property under test -- the file was
        chosen *because* it exercises the case, so that is what is pinned.
        """
        disabled = [s for s in corpus.sensors if s.disabled_in_config]
        assert disabled, "no disabled-in-config sensor survived the read"
        assert any(s.source.endswith("spc621d8hm3.json") for s in disabled), \
            "the fixture vendored for this case no longer exercises it"

    @pytest.mark.parametrize("filename,bound", [("fbyv2.json", "105"),
                                                ("fbyv35.json", "55")])
    def test_both_upstream_defects_are_found(self, corpus, filename, bound):
        """A contradiction in the expectation source is invisible to anything
        that only watches readings. These are the two the corpus run found."""
        hits = [a for a in corpus.anomalies
                if a.kind == "threshold_direction_conflict"
                and a.source.endswith(filename)]
        assert hits, f"the {filename} defect no longer reproduces"
        assert any(bound in a.detail for a in hits), \
            f"the {filename} defect reproduced with an unexpected bound"

    def test_exactly_two_defects_and_no_more(self, corpus):
        """A count that only grows cannot tell a new finding from a regression in
        the checker. Pinned so a change that starts flagging healthy thresholds
        fails here rather than looking like progress."""
        conflicts = [a for a in corpus.anomalies
                     if a.kind == "threshold_direction_conflict"]
        assert len(conflicts) == 2, [a.source for a in conflicts]


class TestTheGapsAreStated:
    """A fixture set that looks complete is worse than one that admits a gap."""

    def test_the_readme_records_what_is_not_covered(self):
        body = (UPSTREAM / "README.md").read_text()
        assert "What these do NOT cover" in body
        assert "$Name" in body, "the uncovered template variable is not named"
        assert "0ada048303bb" in body, "the fixture README no longer states the pin"

    def test_the_name_variable_is_genuinely_absent_here(self, corpus):
        """Pins the gap itself. If a later fixture adds `$Name` coverage, this
        fails and the README paragraph claiming it is missing must be corrected
        in the same change — otherwise the document outlives the fact."""
        assert not any("$Name" in s.name for s in corpus.sensors), \
            "a fixture now exercises $Name; update the README's gap list"

    def test_no_vendored_entry_carries_an_unrecognised_name_key(self, corpus):
        """The third bucket must be empty for files already vendored. If it fills,
        a newly added fixture uses a channel spelling this reader does not know,
        and the answer is to measure the producer again -- not to widen a pattern
        until the anomaly goes away."""
        unknown = [a for a in corpus.anomalies if a.kind == "unrecognised_name_key"]
        assert unknown == [], [a.detail for a in unknown]


class TestTheEndToEndDiffReproducesFromAClone:
    """The pair that closes acceptance criterion 1's real gap.

    Every other test here proves a *finding* about the declaration. This one runs
    the actual product -- declaration against machine -- with both halves committed
    to the repository: three upstream configuration files at the pin, and a walk
    captured from upstream `bmcweb` under QEMU. No network, no mock, no hardware.

    Pinning exact counts is normally the wrong instinct, and it is right here for
    one reason: both inputs are frozen files in this repository, so the numbers
    cannot drift underneath the test. If they move, the reader moved.
    """

    # The decorator ORDER is load-bearing, and both orders look equally
    # plausible. `@classmethod` on the OUTSIDE -- which is how this was written
    # -- worked only because `classmethod` chained `__get__` to the descriptor
    # beneath it, and CPython removed that chaining in 3.13. From 3.13 on, pytest
    # could not see the fixture marker and both tests below ERRORed with
    # `fixture 'report' not found`: the only two that run declaration against
    # machine end to end, silently not running, while CI pinned a single 3.11
    # and every host this is deployed on runs 3.14.
    #
    # Dropping `@classmethod` also passes, and is a trap: pytest deprecated
    # class-scoped fixtures declared as instance methods and removes them in
    # pytest 10. Only this order satisfies both -- measured on 3.10 and 3.14,
    # against pytest 9.1.
    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.redfish import walk_from_dict
        declaration = load_declaration([str(UPSTREAM / "meta" / "bletchley")])
        walk = walk_from_dict(json.loads(
            (ROOT / "tests" / "fixtures" / "walk_qemu_bletchley.json").read_text()))
        return compare(declaration, walk)

    def test_the_diff_matches_what_the_live_run_produced(self, report):
        """`declared_absent` moved 25 -> 58, and the move is a correction.

        The 25 was measured with a reader that took an entry's rail set from its
        thresholds instead of from its `Labels` array. Bletchley's hot-swap
        controllers and INA230 current monitors declare `pin`, `vin`, `vout1`,
        `iout1`, `power1` and friends with a threshold on none of them, so 33
        declared rails were invisible -- and QEMU emulates none of those parts, so
        every one of them really is absent from the capture.

        The old number was not a different answer to this question. It was this
        question asked about a smaller declaration than the file actually makes.
        """
        from collections import Counter
        kinds = Counter(f.kind for f in report.findings)
        assert kinds["matched_inexactly"] == 28
        assert kinds["declared_absent"] == 58
        assert report.regressions, "a partial emulation should still report absences"

    def test_nothing_the_machine_reported_is_called_undeclared(self, report):
        """The regression pin for the channel defect, in the exact shape it was
        found. Real firmware served MB_U72_THERM_REMOTE and MB_U73_THERM_REMOTE;
        the reader discarded them, and the diff accused upstream's configuration
        of omitting sensors it declares as `Name1`. If this count leaves zero,
        that accusation is back."""
        undeclared = [f for f in report.findings if f.kind == "undeclared_present"]
        assert undeclared == [], [f.sensor for f in undeclared]

    def test_the_quantity_named_channel_is_declared_from_the_pinned_file(self):
        """`bletchley_frontpanel.json` at this pin spells the humidity channel
        `NameHumidity`; later revisions renamed it `Name1`. It is declared here
        only because both spellings are read."""
        declaration = load_declaration([str(UPSTREAM / "meta" / "bletchley")])
        assert "FRONT_PANEL_HUMIDTY" in {s.name for s in declaration.sensors}
