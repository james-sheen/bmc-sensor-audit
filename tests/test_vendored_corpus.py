"""Every documented parser finding, reproduced from files in this repository.

Acceptance criterion 1 was proven by a run nobody else could make: the parser
claims were measured against 247 upstream configurations on a local checkout, by
path. True, and unverifiable by any reader — which for a claim in a public README
is the same as unproven.

`tests/fixtures/upstream/` now carries nine of those files verbatim, chosen by
measuring the corpus for each documented property and taking the smallest file
exhibiting it that also passes this project's hygiene check. This module runs the
shipped reader over them and asserts each finding actually appears.

**These tests fail if the fixtures are edited.** That is the point — a fixture
adjusted until a test passes proves only that the test passes. The files are
third-party content and their whole value is being exactly what upstream ships.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "upstream"
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory.entity_manager import (  # noqa: E402
    ANY_TEMPLATE, load_declaration)


@pytest.fixture(scope="module")
def corpus():
    return load_declaration([str(UPSTREAM)])


class TestTheFixturesArePresentAndLicensed:
    def test_nine_configurations_are_vendored(self):
        assert len(list(UPSTREAM.rglob("*.json"))) == 9

    def test_the_upstream_licence_travels_with_them(self):
        """Apache-2.0 requires the notice to accompany redistribution. Upstream
        spells the file LICENCE; a LICENSE glob misses it, which is how this was
        nearly shipped without one."""
        licence = UPSTREAM / "LICENCE"
        assert licence.is_file(), "upstream licence not redistributed alongside"
        body = licence.read_text()
        assert "Apache License" in body
        assert "Copyright 2018 Intel Corporation" in body

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
        assert corpus.files_read == 9
        assert corpus.unreadable == [], corpus.unreadable
        assert len(corpus.sensors) > 0

    def test_block_comments_are_read_not_skipped(self, corpus):
        """Ten of the 247 are not strict JSON. A tool that skips them reports
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

    def test_an_entry_labels_array_is_not_read(self, corpus):
        """A LEAD, pinned so it cannot be forgotten, not a passing behaviour.

        Upstream marks the rails on a pmbus entry with a `Labels` array. This
        parser never reads that key — expansion comes only from threshold
        labels. Eight entries across four vendored files carry one. A sensor
        declared through `Labels` alone, with no per-rail thresholds, would be
        invisible to this tool.

        Whether that is a defect is unmeasured. This test fails the day the
        parser starts reading the key, which is the moment the question gets an
        answer and this test should be replaced by one asserting the new
        behaviour.
        """
        from bmc_sensor_audit.inventory import entity_manager
        source = Path(entity_manager.__file__).read_text()
        assert '"Labels"' not in source and "'Labels'" not in source, \
            "the parser now reads the Labels array — re-measure whether any " \
            "sensor was being missed, and update the fixture README's lead"

        delta = [s for s in corpus.sensors
                 if s.source.endswith("delta_awf2dc3200w_psu.json")]
        assert delta, "the ignored-Labels example declares nothing at all"
        assert not any(s.label for s in delta), \
            "delta now yields labelled sensors; the lead has changed shape"

    @pytest.mark.parametrize("token,filename", [
        ("$index", "8x25_hsbp.json"),
        ("$ipmbindex", "twinlake.json"),
        ("$bus", "santabarbara_sitv_eth.json"),
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
                 if s.source.endswith("santabarbara_sitv_eth.json")]
        compound = [n for n in names if "$bus_" in n]
        assert compound, "the compound-template fixture no longer exercises the case"

    def test_disabled_entries_are_declared_but_marked(self, corpus):
        """Present-but-disabled is the middle value of a three-valued presence,
        and the case the tool exists for. It must not be dropped at read time."""
        disabled = [s for s in corpus.sensors if s.disabled_in_config]
        assert disabled, "no disabled-in-config sensor survived the read"
        assert all(s.source.endswith("asrock_spc621d8hm3.json") for s in disabled)

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
        assert "No upstream revision is pinned" in body

    def test_the_name_variable_is_genuinely_absent_here(self, corpus):
        """Pins the gap itself. If a later fixture adds `$Name` coverage, this
        fails and the README paragraph claiming it is missing must be corrected
        in the same change — otherwise the document outlives the fact."""
        assert not any("$Name" in s.name for s in corpus.sensors), \
            "a fixture now exercises $Name; update the README's gap list"
