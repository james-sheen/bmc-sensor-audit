"""The door a vertical supplies its vocabulary through, and its failure modes.

The seam is only a seam if the BUNDLED vertical uses it too. A private path in
for the one that ships would make this a naming convention, so the tests below
check the bundled vertical through the same entry point an outside one uses.

Every test that leaves something registered resets the registry. A module-level
registry that leaks between tests turns a later failure into a mystery.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bmc_sensor_audit.core import plugins, vocabulary
from bmc_sensor_audit.core.vocabulary import PluginError, VocabularyNotRegistered
from bmc_sensor_audit.inventory import sensor_types
from bmc_sensor_audit.verticals import bmc

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _empty_registry():
    """Empty for the duration, and RESTORED after.

    Resetting to empty on the way out would leave the rest of the suite without
    the vocabulary `conftest` registered for it, and the first casualty would be
    whichever file happens to run next -- a failure that reads as unrelated.
    """
    previous = vocabulary._REGISTERED
    vocabulary.reset()
    yield
    vocabulary._REGISTERED = previous


class TestAnEmptyRegistryRefuses:
    def test_asking_before_anyone_registered_raises_with_the_remedy(self):
        with pytest.raises(VocabularyNotRegistered) as caught:
            vocabulary.current()
        message = str(caught.value)
        assert "bmc_sensor_audit.plugins" in message, "the refusal does not say where a vocabulary comes from"
        assert "--plugin" in message

    def test_no_entry_points_leaves_it_empty_rather_than_defaulting(self):
        """The failure this guards is the quiet one: a run that classifies
        nothing and reports cleanly."""
        plugins.load_all(entry_points=False, environment=False)
        assert not vocabulary.registered()
        with pytest.raises(VocabularyNotRegistered):
            vocabulary.current()


class TestTheBundledVerticalGoesThroughTheDoor:
    def test_the_entry_point_is_declared_for_it(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '[project.entry-points."bmc_sensor_audit.plugins"]' in text, (
            "the bundled vertical has no entry point, so an installed consumer "
            "reaches it only by importing it directly")
        assert re.search(r'^bmc\s*=\s*"bmc_sensor_audit\.verticals\.bmc:register"',
                         text, re.M), "the declared target moved"

    def test_the_declared_target_exists_and_registers(self):
        """The declaration above is a string in a file. This calls it."""
        said = bmc.register()
        assert vocabulary.registered()
        assert said, "a vertical that registers silently cannot be reported in a run header"

    def test_it_delegates_rather_than_answering_on_its_own(self):
        bmc.register()
        supplied = vocabulary.current()
        checked = 0
        for declared_type in sorted(sensor_types.KNOWN_SENSOR)[:5] + ["Temperature", None]:
            assert supplied.classify(declared_type) == sensor_types.classify(declared_type)
            assert supplied.is_expected_live(declared_type) == sensor_types.is_expected_live(declared_type)
            checked += 1
        assert checked >= 6, "the delegation check ran on too few types to mean anything"

    def test_it_carries_the_report_keys_the_shipped_output_already_uses(self):
        """The count keys are domain words and belong to the vertical. If they
        moved, a consumer's report would change shape."""
        bmc.register()
        assert dict(vocabulary.current().count_keys) == {
            sensor_types.NOT_A_SENSOR: "not_a_sensor",
            sensor_types.UNRECOGNISED: "unrecognised_type",
        }


class TestRegistrationRefusesWhatCannotWork:
    class _NoKinds:
        kinds = ()
        count_keys = {}
        def classify(self, t): return "x"
        def is_expected_live(self, t): return True

    class _KeysWithoutKinds:
        kinds = ("only",)
        count_keys = {"absent": "absent_count"}
        def classify(self, t): return "only"
        def is_expected_live(self, t): return True

    def test_an_empty_vocabulary_is_refused(self):
        with pytest.raises(PluginError, match="no kinds"):
            vocabulary.register(self._NoKinds())

    def test_a_count_key_with_no_kind_behind_it_is_refused(self):
        with pytest.raises(PluginError, match="cannot produce"):
            vocabulary.register(self._KeysWithoutKinds())


class TestAFailingVerticalIsAFailure:
    def test_a_spec_that_does_not_import_is_named(self, tmp_path):
        with pytest.raises(PluginError) as caught:
            plugins.load_spec("bmc_sensor_audit.no_such_vertical")
        assert "no_such_vertical" in str(caught.value)

    def test_a_vertical_that_raises_while_registering_is_named(self, tmp_path):
        broken = tmp_path / "broken_vertical.py"
        broken.write_text("def register():\n    raise ValueError('no thanks')\n",
                          encoding="utf-8")
        with pytest.raises(PluginError) as caught:
            plugins.load_spec(str(broken))
        message = str(caught.value)
        assert "broken_vertical" in message and "no thanks" in message, (
            "a broken vertical must be reported with its own name and reason, "
            f"not as a traceback from somewhere else: {message}")
        assert not vocabulary.registered(), (
            "a vertical that raised left a registration behind")

    def test_a_module_without_the_callable_is_named(self, tmp_path):
        empty = tmp_path / "silent_vertical.py"
        empty.write_text("VALUE = 1\n", encoding="utf-8")
        with pytest.raises(PluginError, match="register"):
            plugins.load_spec(str(empty))


class TestTheCliTreatsItAsARefusal:
    def test_plugin_error_is_in_the_refusal_tuple(self):
        """Otherwise a broken vertical reaches the operator as a traceback."""
        from bmc_sensor_audit import cli
        assert PluginError in cli.REFUSALS

    def test_the_flags_exist_on_the_top_level_parser(self):
        from bmc_sensor_audit import cli
        text = cli.build_parser().format_help()
        assert "--plugin" in text and "--no-entry-points" in text
