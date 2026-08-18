"""Read what a platform DECLARES: the entity-manager configuration.

This is the half of the comparison no monitoring stack reads. A BMC can only
report sensors it discovered, and a failed probe is indistinguishable from a
component that was never fitted -- so the declaration is the only place the
expected set exists.

Everything in this module was shaped by measuring the upstream corpus (247
configuration files) rather than by reading the format documentation. Five
things the documented example does not prepare you for, each of which silently
corrupts a naive parser:

1.  **The files are not all strict JSON.** Ten of the 247 carry C-style block
    comments. `json.load` raises on them, and a tool that skips unreadable
    files reports their sensors as absent from the declaration rather than as
    unread -- a false clean bill of health for the whole board.

2.  **The top level is a dict in 178 files and a list in 59.**

3.  **One `Exposes` entry can declare several sensors.** A pmbus or ADM1278
    hot-swap controller carries a `Label` per rail -- `vin`, `iout1`, `pin`,
    `temp1` -- and 748 entries in the corpus use them, one with 33. Counting
    `Exposes` entries counts boards, not sensors.

4.  **A multi-channel part names its other channels `Name1`, `Name2`, ...**
    A TMP421 has a local and a remote input; the configuration writes `Name`
    and `Name1`, and a reader that takes only `Name` discards the rest. 115
    entries carry at least one, the suffixes run to `Name17`, and this is a
    different axis from `Label`: a threshold's `Index` binds it to a channel,
    while `Label` binds it to a rail. Found only by capturing a real board,
    which reported a sensor this reader had dropped -- no fixture could show
    it, because the fixtures are generated from this reader.

5.  **Roughly one name in eight is a template.** `$bus`, `$ADDRESS`, `$index`
    are substituted at runtime, so the declared string never appears on the
    machine. Compared literally, ~470 sensors read as missing on every healthy
    board. They are marked here and handled by the diff, not silently dropped.

The threshold vocabulary is deliberately OPEN. `Direction` is authoritative for
which side of the reading a threshold guards, because it has exactly two values
across all 10,687 thresholds in the corpus and the `Name` field has fifteen.
A name whose severity level is not recognised is reported, never discarded: a
closed enum with a missing member produces a confident misclassification rather
than an error, which is the failure this tool exists to catch in other systems.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "Threshold", "DeclaredSensor", "Anomaly", "Declaration",
    "load_declaration", "parse_config_text",
    "TEMPLATE_VARS", "KNOWN_TEMPLATE", "ANY_TEMPLATE",
]

# `Direction` guards which side of the reading the threshold sits on. Two values,
# no exceptions found in the corpus.
_DIRECTION_BOUND = {"greater than": "upper", "less than": "lower"}

# Severity LEVEL, parsed from the threshold's name. Order matters: the longest
# match wins, so `non recoverable` is not read as `recoverable`. Hyphen and space
# are both in use for the same level, and the corpus contains both spellings.
_LEVEL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"non[ -]?recoverable", "non_recoverable"),
    (r"hard ?shutdown", "hard_shutdown"),
    (r"soft ?shutdown", "soft_shutdown"),
    (r"non[ -]?critical", "warning"),
    (r"critical", "critical"),
    (r"warning", "warning"),
)

# Tokens that assert a bound in the NAME. Only used to cross-check `Direction`;
# never to decide, because two entries in the corpus disagree with themselves.
_NAME_UPPER = re.compile(r"\b(upper|higher)\b", re.I)
_NAME_LOWER = re.compile(r"\blower\b", re.I)

# entity-manager's runtime substitution variables, and the single place they are
# defined. `CONFIG_FORMAT.md` documents three; the corpus uses five. Measured,
# not transcribed: all 30 distinct `$`-tokens across the 247 configuration files
# resolve to exactly these, and the two undocumented ones would otherwise be
# read as literal text that never matches anything.
#
# Longest-first is load-bearing for consumers that strip these: `index` would
# otherwise claim the front of `ipmbindex`.
TEMPLATE_VARS: tuple[str, ...] = ("ipmbindex", "address", "index", "bus", "name")

# A known variable, and any `$`-token at all. The second is deliberately wider
# than the first: a name carrying an unknown variable must be *detectable* as
# templated even though nothing can be substituted into it.
KNOWN_TEMPLATE = re.compile(r"\$(?:" + "|".join(TEMPLATE_VARS) + r")", re.I)
ANY_TEMPLATE = re.compile(r"\$\w+")

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# A multi-channel part names its extra channels alongside `Name`: a TMP421 carries
# a local and a remote channel, and the configuration writes `Name` and `Name1`.
# The suffix is the hwmon channel index minus one -- `Name` is channel 1, `Name1`
# is channel 2 -- which is what a threshold's `Index` refers to.
#
# Derived from the corpus, not from the documentation, which does not mention the
# convention at all: 115 entries carry at least one, the suffixes run to `Name17`,
# and reading only `Name` discards every channel after the first.
_CHANNEL_KEY = re.compile(r"^Name(\d+)$")


@dataclass(frozen=True)
class Threshold:
    """One declared threshold, classified without being normalised away."""

    name: str                  # verbatim, as written in the config
    direction: str             # verbatim: "greater than" / "less than"
    value: float
    severity: int | None
    label: str | None          # pmbus rail, when the entry declares several
    bound: str | None          # "upper" / "lower", from `direction`
    level: str | None          # "critical" / "warning" / ..., from `name`
    index: int | None = None   # hwmon channel this guards, when the entry has several

    @property
    def is_upper(self) -> bool:
        return self.bound == "upper"

    @property
    def is_lower(self) -> bool:
        return self.bound == "lower"


@dataclass(frozen=True)
class DeclaredSensor:
    """A sensor the configuration says this platform exposes."""

    name: str                  # verbatim, template markers intact
    type: str | None           # "ADC", "TMP75", "I2CFan", ...
    label: str | None
    record: str | None         # the board record's own Name
    source: str                # path of the config that declared it
    thresholds: tuple[Threshold, ...] = ()
    disabled_in_config: bool = False

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity within a declaration. A labelled entry is several sensors."""
        return (self.name, self.label)

    @property
    def display_name(self) -> str:
        return f"{self.name}:{self.label}" if self.label else self.name

    @property
    def is_templated(self) -> bool:
        """The declared name contains a runtime substitution, so it will never
        match a live name literally."""
        return bool(ANY_TEMPLATE.search(self.name))

    def bounds(self, bound: str, level: str) -> list[Threshold]:
        return [t for t in self.thresholds if t.bound == bound and t.level == level]


@dataclass(frozen=True)
class Anomaly:
    """Something wrong in the declaration itself, found while reading it.

    These are findings in their own right and the first thing this tool produces
    that the monitoring stack cannot: a defect in the expectation source is
    invisible to anything that only watches readings.
    """

    kind: str
    source: str
    detail: str
    sensor: str | None = None

    def __str__(self) -> str:
        where = f"{self.source}: {self.sensor}" if self.sensor else self.source
        return f"[{self.kind}] {where} -- {self.detail}"


@dataclass
class Declaration:
    """Everything the configuration set says, plus everything wrong with it."""

    sensors: list[DeclaredSensor] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    files_read: int = 0

    def __iter__(self) -> Iterator[DeclaredSensor]:
        return iter(self.sensors)

    def __len__(self) -> int:
        return len(self.sensors)

    @property
    def templated(self) -> list[DeclaredSensor]:
        return [s for s in self.sensors if s.is_templated]

    @property
    def disabled(self) -> list[DeclaredSensor]:
        return [s for s in self.sensors if s.disabled_in_config]

    def by_key(self) -> dict[tuple[str, str | None], DeclaredSensor]:
        return {s.key: s for s in self.sensors}


def _strip_block_comments(text: str) -> str:
    """Remove C-style comments, which entity-manager tolerates and JSON does not.

    Only block comments appear in the corpus. Line comments are not stripped: no
    file uses them, and `//` occurs inside D-Bus paths and URLs where removing
    the rest of the line would corrupt the value.
    """
    return _BLOCK_COMMENT.sub("", text)


def _classify_level(name: str) -> str | None:
    lowered = name.lower()
    for pattern, level in _LEVEL_PATTERNS:
        if re.search(pattern, lowered):
            return level
    return None


def _name_asserts_bound(name: str) -> str | None:
    if _NAME_UPPER.search(name):
        return "upper"
    if _NAME_LOWER.search(name):
        return "lower"
    return None


def _read_thresholds(
    entry: dict[str, Any], sensor_name: str, source: str, anomalies: list[Anomaly]
) -> dict[str | None, list[Threshold]]:
    """Parse an entry's thresholds, grouped by label. Records what it cannot classify."""
    by_label: dict[str | None, list[Threshold]] = {}
    for raw in entry.get("Thresholds") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("Name", ""))
        direction = str(raw.get("Direction", ""))
        bound = _DIRECTION_BOUND.get(direction)
        level = _classify_level(name)

        if bound is None:
            anomalies.append(Anomaly(
                "unknown_threshold_direction", source,
                f"threshold {name!r} has Direction {direction!r}, which is neither "
                f"'greater than' nor 'less than'; the bound cannot be determined",
                sensor_name))
        if level is None:
            anomalies.append(Anomaly(
                "unclassified_threshold_level", source,
                f"threshold name {name!r} does not name a severity level this "
                f"tool recognises; it is carried through unclassified rather "
                f"than dropped, but no rule will act on it",
                sensor_name))

        claimed = _name_asserts_bound(name)
        if claimed and bound and claimed != bound:
            anomalies.append(Anomaly(
                "threshold_direction_conflict", source,
                f"threshold is named {name!r} but its Direction is {direction!r}, "
                f"so it guards the {bound} side while its name says {claimed}. "
                f"A reading on the wrong side of {raw.get('Value')!r} will alarm "
                f"and the condition the name describes will not",
                sensor_name))

        try:
            value = float(raw.get("Value"))
        except (TypeError, ValueError):
            anomalies.append(Anomaly(
                "unreadable_threshold_value", source,
                f"threshold {name!r} has a non-numeric Value {raw.get('Value')!r}",
                sensor_name))
            continue

        label = raw.get("Label")
        label = str(label) if label is not None else None
        severity = raw.get("Severity")
        raw_index = raw.get("Index")
        by_label.setdefault(label, []).append(Threshold(
            name=name, direction=direction, value=value,
            severity=int(severity) if isinstance(severity, int) else None,
            label=label, bound=bound, level=level,
            index=raw_index if isinstance(raw_index, int) else None))
    return by_label


def _channel_names(entry: dict[str, Any]) -> list[tuple[int, str]]:
    """Every channel this entry names, as (hwmon index, name), lowest index first.

    `Name` is channel 1 and `Name<k>` is channel k+1. Keys are matched by pattern
    rather than against a written-down list, because the corpus runs to `Name17`
    and any transcribed ceiling silently drops everything above it.
    """
    found: list[tuple[int, str]] = []
    for key, value in entry.items():
        if key == "Name":
            index = 1
        else:
            match = _CHANNEL_KEY.match(key)
            if match is None:
                continue
            index = int(match.group(1)) + 1
        if isinstance(value, str) and value:
            found.append((index, value))
    return sorted(found)


def _read_entry(
    entry: dict[str, Any], record: str | None, source: str, anomalies: list[Anomaly]
) -> Iterator[DeclaredSensor]:
    """Yield one DeclaredSensor per rail, per channel, or a single plain one.

    Two different conventions let one entry declare several sensors, and they are
    not the same axis. `Label` fans a pmbus part out over its rails. `Name1`,
    `Name2`, ... name the further channels of a multi-channel part such as a
    TMP421's remote input. Where both appear on one entry the resolution is
    device-class specific -- a `Labels` list can select which channels exist at
    all -- and cannot be settled from the configuration alone, so that case is
    reported rather than guessed at.
    """
    channels = _channel_names(entry)
    if not channels:
        return
    primary = channels[0][1]
    sensor_type = entry.get("Type")
    disabled = str(entry.get("Status", "")).lower() == "disabled"
    grouped = _read_thresholds(entry, primary, source, anomalies)

    labels = [k for k in grouped if k is not None]
    # `Labels` can be present without any threshold carrying one, and it still
    # means the part is addressed per rail.
    label_driven = bool(labels) or bool(entry.get("Labels"))

    if len(channels) > 1 and label_driven:
        anomalies.append(Anomaly(
            "ambiguous_channel_naming", source,
            f"entry names {len(channels)} channels "
            f"({', '.join(name for _, name in channels)}) and is also addressed "
            f"per rail, so which channels exist and what each is called depends "
            f"on the device class rather than on this file. Only {primary!r} is "
            f"counted; any further channel is neither declared nor diffed",
            primary))

    if labels:
        # A labelled entry declares one sensor per rail. Any unlabelled
        # thresholds on the same entry apply to all of them.
        shared = tuple(grouped.get(None, ()))
        for label in labels:
            yield DeclaredSensor(
                name=primary, type=sensor_type, label=label, record=record,
                source=source, thresholds=tuple(grouped[label]) + shared,
                disabled_in_config=disabled)
        return

    if len(channels) > 1 and label_driven:
        yield DeclaredSensor(
            name=primary, type=sensor_type, label=None, record=record,
            source=source, thresholds=tuple(grouped.get(None, ())),
            disabled_in_config=disabled)
        return

    # One sensor per channel. A threshold carrying `Index` guards that channel
    # alone; one carrying none guards the whole entry. Index filtering is applied
    # only when there is more than one channel, so a single-channel entry keeps
    # every threshold it has always kept.
    unlabelled = tuple(grouped.get(None, ()))
    seen: set[str] = set()
    for index, name in channels:
        if name in seen:
            anomalies.append(Anomaly(
                "duplicate_channel_name", source,
                f"channels {index} and earlier are both named {name!r}, so they "
                f"cannot be told apart in a diff; the later one is dropped",
                name))
            continue
        seen.add(name)
        thresholds = unlabelled if len(channels) == 1 else tuple(
            t for t in unlabelled if t.index is None or t.index == index)
        yield DeclaredSensor(
            name=name, type=sensor_type, label=None, record=record, source=source,
            thresholds=thresholds, disabled_in_config=disabled)


def parse_config_text(text: str, source: str = "<text>") -> Declaration:
    """Parse one configuration document. Comment-tolerant; accepts dict or list."""
    result = Declaration()
    try:
        data = json.loads(_strip_block_comments(text))
    except json.JSONDecodeError as exc:
        result.unreadable.append((source, f"not parseable as JSON: {exc}"))
        return result

    result.files_read = 1
    records = data if isinstance(data, list) else [data]
    for record in records:
        if not isinstance(record, dict):
            result.unreadable.append((source, "record is not an object"))
            continue
        exposes = record.get("Exposes")
        if exposes is None:
            continue
        if not isinstance(exposes, list):
            result.anomalies.append(Anomaly(
                "malformed_exposes", source,
                f"Exposes is {type(exposes).__name__}, not a list; no sensor in "
                f"this record can be read"))
            continue
        record_name = record.get("Name") if isinstance(record.get("Name"), str) else None
        for entry in exposes:
            if isinstance(entry, dict):
                result.sensors.extend(
                    _read_entry(entry, record_name, source, result.anomalies))
    return result


def load_declaration(paths: Iterable[str | Path], *, pattern: str = "*.json") -> Declaration:
    """Load every configuration under the given files or directories.

    A directory is walked recursively. **An unreadable file is recorded, never
    skipped silently** -- its sensors would otherwise be indistinguishable from
    sensors the platform does not declare, which turns a parse failure into a
    clean bill of health.
    """
    combined = Declaration()
    for path in _expand(paths, pattern):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            combined.unreadable.append((str(path), f"cannot read: {exc}"))
            continue
        one = parse_config_text(text, source=str(path))
        combined.sensors.extend(one.sensors)
        combined.anomalies.extend(one.anomalies)
        combined.unreadable.extend(one.unreadable)
        combined.files_read += one.files_read
    return combined


def _expand(paths: Iterable[str | Path], pattern: str) -> Iterator[Path]:
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            yield from sorted(path.rglob(pattern))
        else:
            yield path
