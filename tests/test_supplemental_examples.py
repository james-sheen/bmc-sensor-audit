"""The shipped supplemental files: one worked example, one template.

A supplemental file states things the machine does not state about itself, so an
example of one is a claim about somebody's hardware. That makes two of them
necessary rather than one:

**`ampere-mtjade.json` is worked.** Every entry is established by the vendored
configuration plus the PMBus command set its labels name, and the test below runs
it against that configuration and against the generator. It declares no redundant
group and no counter -- not because the platform has none, but because neither can
be established from a configuration file, and an example that guessed one would
teach the guess.

**`TEMPLATE.json` is a skeleton, and the load-bearing test is
`test_the_template_refuses_to_run_until_it_is_edited`.** A template that quietly
did nothing would be the worst artifact in this repository: an operator copies it,
runs it, sees no disagreements and concludes the board agrees with itself. So its
placeholder names match no declaration, the name cross-check refuses the run, and
the refusal names every line still to be filled in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_sensor_audit.detect.generator import generate, peer_property
from bmc_sensor_audit.detect.supplemental import (FORMAT, load_supplemental,
                                                  unmatched_names)
from bmc_sensor_audit.inventory.entity_manager import load_declaration
from bmc_sensor_audit.report import supplemental_as_text

EXAMPLES = Path(__file__).parent.parent / "examples" / "supplemental"
MTJADE = Path(__file__).parent / "fixtures" / "upstream" / "ampere" / "mtjade.json"


@pytest.fixture(scope="module")
def mtjade():
    return load_declaration([str(MTJADE)])


@pytest.fixture(scope="module")
def declared_names(mtjade):
    return {s.display_name for s in mtjade.sensors}


class TestEveryShippedFileIsWellFormed:
    def test_they_all_load(self):
        files = sorted(EXAMPLES.glob("*.json"))
        assert files, "no supplemental examples ship"
        for path in files:
            supplemental = load_supplemental(path)
            assert json.loads(path.read_text())["format"] == FORMAT
            assert supplemental.provenance, f"{path.name} says nothing about itself"

    def test_every_entry_carries_a_basis_that_says_something(self):
        """`basis` is required by the loader, so a file cannot ship without one.
        What the loader cannot check is whether it is a sentence."""
        for path in sorted(EXAMPLES.glob("*.json")):
            supplemental = load_supplemental(path)
            bases = ([g.basis for g in supplemental.redundant_groups]
                     + [c.basis for c in supplemental.counters]
                     + [f.basis for f in supplemental.flows])
            for basis in bases:
                assert len(basis) > 60, f"{path.name}: {basis!r} is not a basis"


class TestTheWorkedExampleRuns:
    def test_every_name_it_uses_is_one_the_platform_declares(self, declared_names):
        supplemental = load_supplemental(EXAMPLES / "ampere-mtjade.json")
        assert unmatched_names(supplemental, declared_names) == []

    def test_it_claims_only_what_a_configuration_can_establish(self):
        """No redundant group, no counter. Both are schematic facts.

        The tempting pairing here is PSU0 against PSU1 -- two supplies, same
        readings, obviously redundant. Whether they are paralleled or independent
        is on the schematic, and this repository does not have it.
        """
        supplemental = load_supplemental(EXAMPLES / "ampere-mtjade.json")
        assert supplemental.redundant_groups == []
        assert supplemental.counters == []
        assert len(supplemental.flows) == 2

    def test_each_flow_stays_inside_one_device(self):
        """A flow across two supplies asserts that power entering one leaves the
        other. The check would then hold or fail for reasons about neither."""
        supplemental = load_supplemental(EXAMPLES / "ampere-mtjade.json")
        for flow in supplemental.flows:
            prefix = flow.input.split("_", 1)[0]
            assert all(o.startswith(prefix + "_") for o in flow.outputs), flow

    def test_it_generates_a_conservation_check_on_the_right_entity(self, mtjade):
        supplemental = load_supplemental(EXAMPLES / "ampere-mtjade.json")
        model, manifest = generate(mtjade, supplemental=supplemental)
        indicators = model["domain"]["indicators"]

        entity = manifest.type_for("PSU0_PINPUT")
        assert entity is not None, "the flow input was not modelled"
        block = indicators[entity][0]
        assert "CONSERVATION" in block["axioms"]
        assert block["conservation"]["output_properties"] == [
            peer_property("PSU0_POUTPUT")]

    def test_the_unstated_loss_margin_is_surfaced_rather_than_silent(self):
        """It declares no `loss_margin`, deliberately -- that number is on the
        PSU datasheet. The engine then applies its own, and a report that did not
        say so would present an engine default as an operator's specification."""
        supplemental = load_supplemental(EXAMPLES / "ampere-mtjade.json")
        assert all(f.loss_margin is None for f in supplemental.flows)

        rendered = supplemental_as_text(supplemental)
        assert "did not choose" in rendered
        assert "PSU0_PINPUT" in rendered and "PSU1_PINPUT" in rendered


class TestTheTemplateCannotRunUnedited:
    def test_the_template_refuses_to_run_until_it_is_edited(self, declared_names):
        """The load-bearing test. See the module docstring.

        Every placeholder name is reported, not just the first: an operator
        filling this in wants the whole list, and stopping at one turns editing
        into a guessing game.
        """
        template = load_supplemental(EXAMPLES / "TEMPLATE.json")
        missing = unmatched_names(template, declared_names)
        assert sorted(missing) == sorted(template.names()), (
            "a template name matched a real declaration; it would then create a "
            "real check out of placeholder text")
        assert len(missing) >= 4

    def test_it_still_demonstrates_all_three_kinds(self):
        """A template that showed only the easy one would leave the two hardest
        declarations undocumented by example."""
        template = load_supplemental(EXAMPLES / "TEMPLATE.json")
        assert template.redundant_groups and template.counters and template.flows

    def test_it_says_it_is_a_template_where_a_reader_looks_first(self):
        raw = json.loads((EXAMPLES / "TEMPLATE.json").read_text())
        assert raw["provenance"].startswith("TEMPLATE")
