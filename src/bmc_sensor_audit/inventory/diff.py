"""Compare what is declared against what is reported. This is the product.

Every other module exists to feed this one. The comparison answers a question no
monitoring stack asks, because none of them read the declaration: **which sensors
should be here and are not?**

Three design commitments, each of which costs code and each of which exists
because the cheap version produces a confidently wrong report:

**Matching is layered and always attributed.** A declared name matches a live one
exactly, or after normalisation, or -- for the roughly one name in eight that
carries a runtime template like `$bus` -- through a pattern derived from that
template. Every match records *how* it was made, so a fuzzy match is visible in
the output rather than indistinguishable from an exact one. A tool that silently
fuzzy-matches will eventually pair two unrelated sensors and report a clean
board.

**An incomplete walk is not an empty machine.** If any fetch failed, absence
findings are withheld entirely rather than reported against a partial picture.
A transport failure that renders as `47 sensors missing` is worse than no report,
because someone will act on it.

**Presence is three-valued, not two.** Present and reading, present but disabled
or unreadable, and entirely absent are three different hardware conditions with
three different responses. Collapsing the middle one into either neighbour loses
the case this tool was built for -- the disabled sensor that no UI displays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .entity_manager import (
    ANY_TEMPLATE, KNOWN_TEMPLATE, Declaration, DeclaredSensor,
)
from .redfish import LiveSensor, Walk
from . import sensor_types

__all__ = ["Finding", "Match", "DiffReport", "compare"]

# Findings that mean something got worse. The CLI exits non-zero on these so a
# firmware-upgrade gate can fail a release candidate in CI.
REGRESSION_KINDS = frozenset({
    "declared_absent", "declared_unreadable", "declared_disabled",
    "threshold_missing", "threshold_drift", "threshold_direction_conflict",
    "interface_divergence",
})

_SEPARATORS = re.compile(r"[\s_\-]+")

# The template vocabulary lives in one place -- see entity_manager.TEMPLATE_VARS.
# Defining it twice is how the two copies come to disagree, and the direction
# they disagree in here is the dangerous one: a matcher that recognises fewer
# variables than the reader silently wildcards the difference.
_TEMPLATE, _ANY_TEMPLATE = KNOWN_TEMPLATE, ANY_TEMPLATE


@dataclass(frozen=True)
class Finding:
    kind: str
    sensor: str
    detail: str
    declared_in: str | None = None
    live_path: str | None = None

    @property
    def is_regression(self) -> bool:
        return self.kind in REGRESSION_KINDS

    def __str__(self) -> str:
        return f"[{self.kind}] {self.sensor} -- {self.detail}"


@dataclass(frozen=True)
class Match:
    declared: DeclaredSensor
    live: LiveSensor
    how: str          # "exact" | "normalised" | "template"


@dataclass
class DiffReport:
    findings: list[Finding] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    unmatched_declared: list[DeclaredSensor] = field(default_factory=list)
    unmatched_live: list[LiveSensor] = field(default_factory=list)
    walk_complete: bool = True
    absence_withheld: bool = False
    # Declarations excluded from expectation because their Type does not produce a
    # reading, keyed by kind. Reported, never silently dropped -- an exclusion
    # nobody can see is indistinguishable from a checker that forgot to look.
    not_sensor_kinds: dict[str, list] = field(default_factory=dict)

    @property
    def regressions(self) -> list[Finding]:
        return [f for f in self.findings if f.is_regression]

    @property
    def exit_code(self) -> int:
        """Non-zero when something got worse, so CI can gate on it."""
        return 1 if self.regressions else 0

    def counts(self) -> dict[str, int]:
        reading = sum(1 for m in self.matches if m.live.is_reading)
        return {
            "declared": len(self.matches) + len(self.unmatched_declared),
            "matched": len(self.matches),
            "reading": reading,
            "present_not_reading": len(self.matches) - reading,
            "declared_absent": len(self.unmatched_declared),
            "undeclared_present": len(self.unmatched_live),
            "not_a_sensor": len(self.not_sensor_kinds.get(sensor_types.NOT_A_SENSOR, [])),
            "unrecognised_type": len(self.not_sensor_kinds.get(sensor_types.UNRECOGNISED, [])),
            "findings": len(self.findings),
            "regressions": len(self.regressions),
        }


def _normalise(name: str) -> str:
    return _SEPARATORS.sub("_", name.strip().lower())


def _template_pattern(name: str) -> re.Pattern[str] | None:
    """Turn `$bus_ADC0` into a pattern that matches its substituted form.

    Anchored at both ends, and only a KNOWN variable becomes a wildcard, so the
    literal remainder still has to match: `$bus_ADC0` pairs with `13_ADC0` and
    not with `P12V_AUX`.

    The first cut of this got it wrong in a way worth keeping a note about. It
    wildcarded `\\$[A-Za-z_]\\w*`, and because `\\w` includes the underscore that
    pattern consumed `$bus_ADC0` entirely -- one token, no literal left, and a
    resulting regex of `^.*$` that matched every sensor on the machine. A greedy
    class that eats the separator turns a precise matcher into an indiscriminate
    one, and the failure is silent: every declared sensor pairs with whatever the
    walk returned first, and the board reports clean.

    Returns None when the name carries a variable this tool does not know. An
    unrecognised variable is reported as an unmatched sensor, never wildcarded.
    """
    if not _ANY_TEMPLATE.search(name):
        return None
    parts = [re.escape(p) for p in _TEMPLATE.split(name)]
    if len(parts) == 1:                       # a `$` that named no known variable
        return None
    pattern = r"[\w.:-]+".join(parts)
    if _ANY_TEMPLATE.search(pattern):         # a known variable AND an unknown one
        return None
    return re.compile("^" + pattern + "$", re.I)


def _index_live(walk: Walk) -> tuple[dict[str, LiveSensor], dict[str, LiveSensor]]:
    exact: dict[str, LiveSensor] = {}
    normalised: dict[str, LiveSensor] = {}
    for sensor in walk:
        exact.setdefault(sensor.name, sensor)
        normalised.setdefault(_normalise(sensor.name), sensor)
    return exact, normalised


def _pair(declaration: Iterable[DeclaredSensor], walk: Walk) -> tuple[list[Match], list[DeclaredSensor]]:
    exact, normalised = _index_live(walk)
    claimed: set[str] = set()
    matches: list[Match] = []
    unmatched: list[DeclaredSensor] = []

    # Exact, then normalised, then template -- most confident first, so a
    # template pattern can never steal a sensor an exact name would have claimed.
    pending: list[DeclaredSensor] = []
    for declared in declaration:
        live = exact.get(declared.name)
        if live is not None and live.path not in claimed:
            claimed.add(live.path)
            matches.append(Match(declared, live, "exact"))
        else:
            pending.append(declared)

    still_pending: list[DeclaredSensor] = []
    for declared in pending:
        live = normalised.get(_normalise(declared.name))
        if live is not None and live.path not in claimed:
            claimed.add(live.path)
            matches.append(Match(declared, live, "normalised"))
        else:
            still_pending.append(declared)

    for declared in still_pending:
        pattern = _template_pattern(declared.name)
        hit = None
        if pattern is not None:
            for sensor in walk:
                if sensor.path not in claimed and pattern.match(sensor.name):
                    hit = sensor
                    break
        if hit is not None:
            claimed.add(hit.path)
            matches.append(Match(declared, hit, "template"))
        else:
            unmatched.append(declared)

    return matches, unmatched


def _compare_thresholds(match: Match, findings: list[Finding]) -> None:
    """Does the live sensor carry the thresholds the config declared?

    Drift here is its own finding: a firmware update that keeps a sensor and
    quietly widens its limits is invisible to presence checking and to ordinary
    alerting, because nothing ever breaches a threshold that moved.
    """
    declared, live = match.declared, match.live
    for threshold in declared.thresholds:
        if threshold.bound is None or threshold.level is None:
            continue
        slot = (threshold.bound, threshold.level)
        actual = live.thresholds.get(slot)
        if actual is None:
            findings.append(Finding(
                "threshold_missing", declared.display_name,
                f"config declares a {threshold.bound} {threshold.level} threshold "
                f"at {threshold.value:g}; the live sensor carries none",
                declared.source, live.path))
        elif not _close(actual, threshold.value):
            findings.append(Finding(
                "threshold_drift", declared.display_name,
                f"{threshold.bound} {threshold.level} declared {threshold.value:g}, "
                f"live {actual:g}",
                declared.source, live.path))


def _close(a: float, b: float, *, rel: float = 1e-6) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def _classify_excluded(declared: list) -> dict:
    """Group the declarations that will not be expected live, by why.

    Returned rather than discarded so the report can say how many entries were set
    aside and on what grounds. A filter whose output nobody can inspect is a filter
    nobody can challenge.
    """
    excluded: dict = {}
    for sensor in declared:
        kind = sensor_types.classify(sensor.type)
        if kind != sensor_types.SENSOR:
            excluded.setdefault(kind, []).append(sensor)
    return excluded


def compare(declaration: Declaration, walk: Walk, *,
            include_disabled_in_config: bool = False) -> DiffReport:
    """Diff a declaration against a walk.

    `include_disabled_in_config` controls whether sensors the config itself marks
    `Status: disabled` are expected to be present. They are excluded by default:
    94 entries in the upstream corpus carry that marker, and reporting each as
    missing on a healthy board is precisely the every-run-red noise that teaches
    people to stop reading the report.
    """
    report = DiffReport(walk_complete=walk.complete)
    findings: list[Finding] = []

    # EVERY declared sensor is paired, including the ones the config marks
    # disabled. Excluding them from pairing was the first cut and it was wrong in
    # a way only a run against real data showed: a board whose config disables
    # four fan connectors that the machine nonetheless reports produced four
    # `undeclared_present` rows on a completely healthy walk. Noise on every run
    # is how a report teaches its reader to stop opening it.
    #
    # So they are matched, and only their EXPECTATIONS differ: a config-disabled
    # sensor that is absent is not a finding, and one that is live is a finding
    # of its own -- the configuration and the machine disagree about whether that
    # hardware is switched on.
    all_matches, unmatched_declared = _pair(list(declaration), walk)
    matched_paths = {m.live.path for m in all_matches}
    report.matches = all_matches
    report.unmatched_live = [s for s in walk if s.path not in matched_paths]

    if not include_disabled_in_config:
        unmatched_declared = [s for s in unmatched_declared if not s.disabled_in_config]

    # The same move again, for a bigger population and a worse symptom. An
    # `Exposes` entry is not necessarily a sensor: PID loops, stepwise fan curves,
    # EEPROMs, firmware blobs, muxes and GPIO presence detectors are declared the
    # same way and can never appear in a Redfish Sensors collection. Expecting them
    # made 1,467 of 8,684 upstream declarations permanently absent, which is a red
    # gate on a healthy board -- and on three boards in four.
    #
    # Three-valued, because a closed split would force a Type this build has never
    # seen into whichever bucket the default happens to be. An unrecognised type is
    # counted and REPORTED, never asserted about: claiming a regression for
    # something we cannot classify is the exact false positive being removed here.
    report.not_sensor_kinds = _classify_excluded(unmatched_declared)
    unmatched_declared = [s for s in unmatched_declared
                          if sensor_types.is_expected_live(s.type)]

    # Anything wrong with the declaration itself travels into the report. A
    # defect in the expectation source is a finding no reading-watcher can see.
    for anomaly in declaration.anomalies:
        findings.append(Finding(
            anomaly.kind, anomaly.sensor or "(config)", anomaly.detail, anomaly.source))
    # A sensor the deprecated tree reports and the modern collection omits.
    # Present on one interface, absent from another -- a firmware defect, and one
    # that a tool reading only its preferred tree cannot see at all.
    for name, shape in walk.divergence:
        findings.append(Finding(
            "interface_divergence", name,
            f"reported under the deprecated {shape} tree and absent from the "
            f"Sensors collection on the same chassis; a client reading only the "
            f"current schema does not see this sensor at all",
            None, None))
    for source, reason in declaration.unreadable:
        findings.append(Finding(
            "config_unreadable", "(config)",
            f"{reason} -- every sensor this file declares is unverifiable, not absent",
            source))

    for match in all_matches:
        live = match.live
        name = match.declared.display_name
        if match.declared.disabled_in_config:
            # The config says this hardware is switched off. If the machine is
            # reporting it anyway, the two disagree, and the config is the thing
            # every downstream generator trusts.
            if live.is_reading:
                findings.append(Finding(
                    "disabled_in_config_but_live", name,
                    f"the configuration marks this Status: disabled, and the "
                    f"machine is reporting {live.reading:g}"
                    f"{' ' + live.units if live.units else ''}",
                    match.declared.source, live.path))
            continue
        if not live.is_enabled:
            findings.append(Finding(
                "declared_disabled", name,
                f"declared and present, but the BMC reports State={live.state!r}. "
                f"A disabled sensor is typically invisible in the web UI",
                match.declared.source, live.path))
        elif live.reading is None:
            findings.append(Finding(
                "declared_unreadable", name,
                "declared and present and enabled, but carries no reading",
                match.declared.source, live.path))
        else:
            _compare_thresholds(match, findings)
        if match.how != "exact":
            findings.append(Finding(
                "matched_inexactly", name,
                f"matched to live sensor {live.name!r} by {match.how}, not by an "
                f"exact name; confirm the pairing before trusting its findings",
                match.declared.source, live.path))

    if walk.complete:
        report.unmatched_declared = unmatched_declared
        for declared in unmatched_declared:
            findings.append(Finding(
                "declared_absent", declared.display_name,
                f"declared by {declared.type or 'an entry'} in the configuration "
                f"and not reported by the machine at all",
                declared.source))
        for live in report.unmatched_live:
            findings.append(Finding(
                "undeclared_present", live.name,
                f"reported by the machine at {live.path} and declared nowhere in "
                f"the configuration set",
                None, live.path))
    else:
        # Withheld on purpose. See the module docstring.
        report.absence_withheld = True
        report.unmatched_declared = []
        findings.append(Finding(
            "walk_incomplete", "(walk)",
            f"{len(walk.errors)} fetch(es) failed, so absence cannot be "
            f"distinguished from an unread subtree. Absence findings are "
            f"withheld: first error was {walk.errors[0][0]} ({walk.errors[0][1]})"))

    report.findings = findings
    return report
