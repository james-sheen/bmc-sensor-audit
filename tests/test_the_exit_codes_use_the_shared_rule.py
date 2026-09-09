"""This tool's exit codes, and the rule it composes them with.

The three numbers had two homes: a line in `cli.py` and
`presence_audit.exit_contract`. Two records of one fact drift, and these two
could have drifted silently -- nothing compared them.

The composition had two IMPLEMENTATIONS, which is worse. This file composed with
a bare `max()` and the other vertical composed with a function that handles the
empty case and normalises values outside the three. Both were correct on every
input either could produce; they are not the same rule, and `max()` of nothing
raises while composing no legs must be `2`. A battery whose legs all failed to
be collected has not come out clean.

**What is asserted here is the MECHANISM, deliberately, and that is the claim.**
The behaviour did not change -- every leg this file composes is an internally
produced constant or a contractual `0`/`1`, and the whole suite is unchanged
across the swap. What changed is which rule runs, so which rule runs is what
gets pinned. The rule's own behaviour is covered where it lives.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from presence_audit import exit_contract
from bmc_sensor_audit import cli

SOURCE = pathlib.Path(inspect.getfile(cli)).read_text(encoding="utf-8")


class TestTheNumbersHaveOneHome:

    def test_this_tools_names_carry_the_contracts_values(self):
        assert cli.EXIT_CLEAN == exit_contract.CLEAN
        assert cli.EXIT_REGRESSION == exit_contract.FINDINGS
        assert cli.EXIT_INCOMPLETE == exit_contract.INCOMPLETE

    def test_they_are_read_from_the_contract_and_not_written_out(self):
        """Structural. Comparing the values passes just as well against three
        literals that happen to agree today, which is the state this replaced."""
        tree = ast.parse(SOURCE)
        assigned = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                assigned[node.targets[0].id] = node.value
        for name in ("EXIT_CLEAN", "EXIT_REGRESSION", "EXIT_INCOMPLETE"):
            assert name in assigned, f"{name} is no longer a module-level name"
            value = assigned[name]
            assert isinstance(value, ast.Attribute), (
                f"{name} is assigned {ast.dump(value)[:60]}, not read from the "
                f"shared contract. A literal here is a second copy of a number "
                f"the core already owns")

    def test_the_names_stay_this_tools_own(self):
        """`regression` is what a `1` means HERE. The core calls it `findings`
        because it cannot know what a finding is in somebody's domain, and
        adopting its word would have been the leak in the other direction."""
        assert "EXIT_REGRESSION" in SOURCE


def _command_functions(tree):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_cmd_")]


class TestTheCompositionIsTheSharedOne:

    def test_there_are_commands_to_check(self):
        """NON-VACUITY: the check below quantifies over these, and a rename of
        the `_cmd_` convention would empty it silently."""
        assert len(_command_functions(ast.parse(SOURCE))) >= 3

    def test_no_command_composes_its_exit_code_with_a_bare_max(self):
        offenders = []
        for command in _command_functions(ast.parse(SOURCE)):
            for node in ast.walk(command):
                if (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "max"):
                    offenders.append(f"{command.name}:{node.lineno}")
        assert offenders == [], (
            f"{offenders} compose an exit code with a bare max(). It agrees "
            f"with the contract on every value this file produces and differs "
            f"on the ones it does not: max() of nothing raises, and max(0, 137) "
            f"is 137, which is not a code any contract defines")

    def test_that_check_can_produce_a_positive(self):
        """Before believing a negative, prove the probe can see one -- and that
        it does not fire on a legitimate `max()`, which this file also has."""
        tree = ast.parse("def _cmd_x():\n    return max(a, b)\n"
                         "def _cmd_y():\n    slowest = max(pairs, key=lambda p: p[1])\n"
                         "    return 0\n")
        found = []
        for command in _command_functions(tree):
            for node in ast.walk(command):
                if (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "max"):
                    found.append(command.name)
        assert found == ["_cmd_x"], (
            f"the predicate found {found}: it must see a composed return and "
            f"must not fire on a max() used for something else")

    def test_every_command_that_composes_uses_the_contract(self):
        """The other direction. A command could stop composing altogether and
        the check above would still pass over it."""
        composing = [c.name for c in _command_functions(ast.parse(SOURCE))
                     if "compose" in ast.dump(c)]
        assert len(composing) >= 3, (
            f"only {composing} compose through the contract; the swap covered "
            f"three call sites and this should not have shrunk")


class TestTheRuleThisToolNowRunsIsTheRealOne:
    """Reached through the tool's own import, so binding `compose` to something
    else here would fail even if the core stayed correct."""

    def test_composing_nothing_is_incomplete(self):
        assert cli._exit_contract.compose() == cli.EXIT_INCOMPLETE

    def test_an_out_of_range_leg_cannot_escape(self):
        assert cli._exit_contract.compose(cli.EXIT_CLEAN, 137) == cli.EXIT_INCOMPLETE

    @pytest.mark.parametrize("legs,expected", [
        ((0, 0), 0), ((0, 1), 1), ((1, 2), 2), ((2, 0), 2), ((1, 1), 1)])
    def test_the_worst_leg_still_wins(self, legs, expected):
        assert cli._exit_contract.compose(*legs) == expected
