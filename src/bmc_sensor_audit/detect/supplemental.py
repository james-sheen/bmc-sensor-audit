"""Declarations the machine does not make about itself, written down by an operator.

Two axioms need a fact `entity-manager` has no way to state, and neither can be
derived from the configuration however carefully it is read:

**Redundancy.** CONSISTENCY can check that two readings which should agree do.
Nothing in `entity-manager` says two sensors measure the same thing. The tempting
derivation is the multi-channel part -- a TMP421 declares `Name` and `Name1`, so pair
them -- and it is wrong on the physics: those two channels are the chip's own die and
an external diode, which differ by tens of degrees on a working board. The other
tempting derivation is identical declared thresholds, and the pinned corpus refutes it
outright: `SLED1_THERM_LOCAL` through `SLED6_THERM_LOCAL` carry the same four numbers
and sit on six different parts.

So redundancy is a claim about the hardware, and this project's rule for the shape of
that claim already exists in the engine's own modelling guide: **a floor is a
specification, not a guess.** A pairing is the same kind of sentence. Auto-pairing
would have produced a tool that reports disagreement between two things that were
never the same measurement, which is worse than not checking -- it is a false positive
that looks like the feature working.

**Counters.** MONOTONICITY judges values that only ever climb. Power-on hours and
error counters mostly live outside `entity-manager` altogether, so which readings are
cumulative is also operator knowledge.

**Flows.** CONSERVATION checks that what goes into a device comes out of it, minus a
tolerated loss. `entity-manager` does declare the readings -- the pinned Ampere
Mt.Jade configuration exposes `PSU0_PINPUT` and `PSU0_POUTPUT` -- but nothing in the
file says the second is the first minus conversion loss, and nothing says what loss is
acceptable. **The loss margin is an efficiency figure off a datasheet**, which is the
same kind of number as a threshold floor and gets the same treatment: declared, with
its basis, or absent.

Those two readings also carry **no thresholds at all**, so the generator's ordinary
rule excludes them -- a sensor with nothing to bound against is a question nobody
asked. Naming one in a flow is what asks the question, so a flow participant is
modelled whether or not it has bounds.

## The file

    {
      "format": "bmc-sensor-audit/supplemental/1",
      "provenance": "who established this and how",
      "redundant_groups": [
        {"sensors": ["A", "B"], "tolerance": 0.05,
         "basis": "why these are the same measurement"}
      ],
      "counters": [
        {"sensor": "PWR_ON_HOURS", "direction": "increasing", "allow_reset": true,
         "basis": "why this only climbs"}
      ]
    }

**`basis` is required and is not decoration.** It is the difference between a
specification and a guess, and it is the field a reviewer reads first. A group without
one is refused rather than accepted with a warning, because a warning on a path that
still works is a warning nobody reads.

Sensors are named as the declaration names them -- `display_name`, so a pmbus rail is
`NAME:LABEL`. A name this file mentions and the declaration does not is refused too:
the likeliest cause is a typo, and a typo silently drops the pairing it was written to
create, leaving a file that looks like the check is running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Supplemental", "RedundantGroup", "Counter", "load_supplemental",
           "SupplementalError", "FORMAT"]

FORMAT = "bmc-sensor-audit/supplemental/1"

# The engine's own default is 0.05 relative. Restated rather than imported because
# Stage 1 must not import the engine, and a default that silently tracked an upstream
# constant would move a live tolerance under an operator on an engine bump.
DEFAULT_TOLERANCE = 0.05

_DIRECTIONS = ("increasing", "decreasing")


class SupplementalError(ValueError):
    """The file could not be used. Never a warning: see the module docstring."""


@dataclass(frozen=True)
class RedundantGroup:
    sensors: tuple[str, ...]
    basis: str
    tolerance: float | None = None
    tolerance_absolute: float | None = None

    @property
    def primary(self) -> str:
        """The member that carries the `consistency` block.

        One side declares it, not both. The engine's agreement test is symmetric --
        it divides by `max(abs(a), abs(b))` precisely so that `a agrees with b` means
        the same as `b agrees with a` -- so declaring it twice would produce two
        findings for one disagreement and double-count a single drifting sensor.
        """
        return self.sensors[0]

    @property
    def peers(self) -> tuple[str, ...]:
        return self.sensors[1:]


@dataclass(frozen=True)
class Counter:
    sensor: str
    basis: str
    direction: str = "increasing"
    allow_reset: bool = True


@dataclass(frozen=True)
class Flow:
    """One conservation claim: this input, these outputs, this tolerated loss."""

    input: str
    outputs: tuple[str, ...]
    basis: str
    loss_margin: float | None = None

    @property
    def members(self) -> tuple[str, ...]:
        return (self.input,) + self.outputs


@dataclass
class Supplemental:
    """Operator declarations, and the file they came from."""

    provenance: str = ""
    source: str | None = None
    redundant_groups: list[RedundantGroup] = field(default_factory=list)
    counters: list[Counter] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.redundant_groups or self.counters or self.flows)

    def flow_for(self, display_name: str) -> Flow | None:
        """The flow this sensor is the INPUT of, if any. The outputs are carried as
        properties on the input's entity, the same way redundant peers are."""
        for flow in self.flows:
            if flow.input == display_name:
                return flow
        return None

    def modelled_regardless(self) -> set[str]:
        """Sensors that must be modelled even with nothing to bound against.

        A flow's readings routinely carry no thresholds -- the pinned Mt.Jade PSU
        entries declare `pin` and `pout1` with bounds on neither -- and the ordinary
        exclusion rule would drop them, leaving a declared conservation check that
        silently never runs.
        """
        return {name for flow in self.flows for name in flow.members}

    def group_for(self, display_name: str) -> RedundantGroup | None:
        """The group this sensor leads, if it leads one."""
        for group in self.redundant_groups:
            if group.primary == display_name:
                return group
        return None

    def peer_of_any_group(self, display_name: str) -> bool:
        return any(display_name in g.peers for g in self.redundant_groups)

    def counter_for(self, display_name: str) -> Counter | None:
        for counter in self.counters:
            if counter.sensor == display_name:
                return counter
        return None

    def names(self) -> set[str]:
        """Every sensor name this file mentions, for the cross-check against the
        declaration."""
        named = {s for group in self.redundant_groups for s in group.sensors}
        named |= {name for flow in self.flows for name in flow.members}
        return named | {c.sensor for c in self.counters}


def _require(block: dict, key: str, where: str):
    value = block.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SupplementalError(
            f"{where} has no {key!r}. This file states things the machine does not "
            f"state about itself, so every entry has to say what establishes it")
    return value


def load_supplemental(path: str | Path) -> Supplemental:
    """Read and validate a supplemental declarations file.

    Every refusal here is a hard error. A malformed entry that loaded anyway would
    produce a run that reports no disagreements because it never checked for any --
    indistinguishable, from the outside, from a board where everything agrees.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except OSError as error:
        raise SupplementalError(f"{path}: cannot be read: {error}") from error
    except json.JSONDecodeError as error:
        raise SupplementalError(f"{path}: not parseable as JSON: {error}") from error

    if not isinstance(raw, dict):
        raise SupplementalError(f"{path}: top level is not an object")
    declared_format = raw.get("format")
    if declared_format != FORMAT:
        raise SupplementalError(
            f"{path}: format is {declared_format!r}, this build reads {FORMAT!r}")

    result = Supplemental(provenance=str(raw.get("provenance") or ""),
                          source=str(path))

    for index, block in enumerate(raw.get("redundant_groups") or []):
        where = f"{path}: redundant_groups[{index}]"
        if not isinstance(block, dict):
            raise SupplementalError(f"{where} is not an object")
        sensors = block.get("sensors")
        if not isinstance(sensors, list) or len(sensors) < 2:
            raise SupplementalError(
                f"{where} names {sensors!r}; a redundant group needs at least two "
                f"sensors, because the claim is that they agree with each other")
        if len(set(sensors)) != len(sensors):
            raise SupplementalError(
                f"{where} names the same sensor twice; a reading always agrees with "
                f"itself, so the check would pass while measuring nothing")
        tolerance = block.get("tolerance")
        absolute = block.get("tolerance_absolute")
        if tolerance is not None and absolute is not None:
            raise SupplementalError(
                f"{where} sets both `tolerance` and `tolerance_absolute`. The engine "
                f"reads the absolute one and ignores the relative one, so the number "
                f"written here would not be the number applied")
        result.redundant_groups.append(RedundantGroup(
            sensors=tuple(str(s) for s in sensors),
            basis=str(_require(block, "basis", where)),
            tolerance=None if tolerance is None else float(tolerance),
            tolerance_absolute=None if absolute is None else float(absolute)))

    for index, block in enumerate(raw.get("flows") or []):
        where = f"{path}: flows[{index}]"
        if not isinstance(block, dict):
            raise SupplementalError(f"{where} is not an object")
        outputs = block.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise SupplementalError(
                f"{where} names outputs {outputs!r}; a flow needs at least one, "
                f"because the claim is that the input arrives at them")
        input_name = str(_require(block, "input", where))
        if input_name in outputs:
            raise SupplementalError(
                f"{where} names {input_name!r} as both input and output; the "
                f"balance would then compare a reading against itself and always "
                f"hold, whatever the device is doing")
        margin = block.get("loss_margin")
        if margin is not None and not 0 <= float(margin) < 1:
            raise SupplementalError(
                f"{where} sets loss_margin {margin!r}; it is a FRACTION of the "
                f"input, so 0.15 means fifteen percent. A value of 1 or more "
                f"tolerates losing everything and can never report a violation")
        result.flows.append(Flow(
            input=input_name,
            outputs=tuple(str(o) for o in outputs),
            basis=str(_require(block, "basis", where)),
            loss_margin=None if margin is None else float(margin)))

    for index, block in enumerate(raw.get("counters") or []):
        where = f"{path}: counters[{index}]"
        if not isinstance(block, dict):
            raise SupplementalError(f"{where} is not an object")
        direction = str(block.get("direction") or "increasing")
        if direction not in _DIRECTIONS:
            raise SupplementalError(
                f"{where} declares direction {direction!r}; this build knows "
                f"{list(_DIRECTIONS)}")
        result.counters.append(Counter(
            sensor=str(_require(block, "sensor", where)),
            basis=str(_require(block, "basis", where)),
            direction=direction,
            allow_reset=bool(block.get("allow_reset", True))))

    return result


def unmatched_names(supplemental: Supplemental, declared: set[str]) -> list[str]:
    """Names the file mentions that the declaration does not carry.

    Returned rather than raised, so the caller can report every one of them at once
    instead of stopping at the first. A typo here is silent by nature: the pairing it
    was meant to create simply never exists, and the run reports no disagreement
    because it asked no question.
    """
    return sorted(supplemental.names() - declared)
