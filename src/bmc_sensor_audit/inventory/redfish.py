"""Read what a machine ACTUALLY reports: a Redfish walk.

Deliberately built on `urllib` from the standard library. Stage 1 is specified to
carry no external dependency, and that property is worth more than the ergonomics
of an HTTP client: it means the coverage diff can be run from a bring-up bench,
a CI runner or a jump host without anyone provisioning anything first.

**Probe the tree; do not assume its shape.** `Thermal` and `Power` are deprecated
in favour of `Sensors`, `ThermalSubsystem` and `PowerSubsystem`, and real fleets
run both -- often at different firmware levels on the same SKU, which is exactly
the population this tool is pointed at. The walker reads the links actually
present on each chassis and branches, rather than requesting one shape and
treating a 404 as an empty machine. An absent sensor and an unwalked subtree look
identical in a report that does not make the distinction.

**Unreachable is not empty.** Every failure to fetch is recorded. A tool whose
job is to notice absence must never let its own transport failure masquerade as
a finding: if the walk is incomplete, the diff has to know, or it will report a
whole chassis of healthy sensors as missing.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = ["LiveSensor", "Walk", "RedfishClient", "walk_chassis", "read_sensor_object",
           "walk_from_dict", "order_walks", "WALK_FORMAT"]

WALK_FORMAT = "bmc-sensor-audit/walk/1"

# Redfish reports a reading and the four thresholds as nested objects on the
# modern schema, and as flat siblings on the deprecated one. Both are read.
_MODERN_THRESHOLDS = {
    "UpperCritical": ("upper", "critical"),
    "UpperCautionUser": ("upper", "warning"),
    "UpperCaution": ("upper", "warning"),
    "LowerCritical": ("lower", "critical"),
    "LowerCautionUser": ("lower", "warning"),
    "LowerCaution": ("lower", "warning"),
    "UpperFatal": ("upper", "non_recoverable"),
    "LowerFatal": ("lower", "non_recoverable"),
}
_LEGACY_THRESHOLDS = {
    "UpperThresholdCritical": ("upper", "critical"),
    "UpperThresholdNonCritical": ("upper", "warning"),
    "UpperThresholdFatal": ("upper", "non_recoverable"),
    "LowerThresholdCritical": ("lower", "critical"),
    "LowerThresholdNonCritical": ("lower", "warning"),
    "LowerThresholdFatal": ("lower", "non_recoverable"),
}


@dataclass(frozen=True)
class LiveSensor:
    """One sensor as the machine reports it, right now."""

    name: str
    path: str                            # the Redfish URI it was read from
    reading: float | None = None
    units: str | None = None
    state: str | None = None             # Status.State
    health: str | None = None            # Status.Health
    thresholds: dict[tuple[str, str], float] = field(default_factory=dict)
    source_shape: str = "sensors"        # "sensors" | "thermal" | "power"

    @property
    def is_enabled(self) -> bool:
        """Absent State is treated as enabled: the schema makes it optional and
        most implementations omit it on healthy sensors."""
        return self.state is None or self.state == "Enabled"

    @property
    def is_reading(self) -> bool:
        return self.is_enabled and self.reading is not None

    @property
    def condition(self) -> str:
        if not self.is_enabled:
            return f"disabled ({self.state})"
        if self.reading is None:
            return "enabled, no reading"
        return "reading"


@dataclass
class Walk:
    """The result of walking one target. Incompleteness is first-class."""

    sensors: list[LiveSensor] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    chassis: list[str] = field(default_factory=list)
    shapes_seen: set[str] = field(default_factory=set)
    divergence: list[tuple[str, str]] = field(default_factory=list)
    # (path, seconds) per fetch. Empty for a walk rehydrated from a capture
    # taken before this was recorded -- absent and zero are different facts,
    # and a missing measurement must not read as an instant response.
    latencies: list[tuple[str, float]] = field(default_factory=list)
    # When the walk was taken, UTC ISO 8601. `None` for a capture written before
    # this field existed, which is a different fact from a walk taken at an unknown
    # time -- see `order_walks`, which refuses to guess for either.
    captured_at: str | None = None

    @property
    def complete(self) -> bool:
        """False if any fetch failed. A diff against an incomplete walk cannot
        distinguish an absent sensor from an unread subtree."""
        return not self.errors

    def by_name(self) -> dict[str, LiveSensor]:
        return {s.name: s for s in self.sensors}

    def __len__(self) -> int:
        return len(self.sensors)

    def __iter__(self) -> Iterator[LiveSensor]:
        return iter(self.sensors)

    def to_dict(self) -> dict[str, Any]:
        """Serialise a walk so it can be re-diffed later without the hardware.

        **The parse is the redaction, and that is deliberate.** What gets written
        is the parsed `LiveSensor` set, never the raw Redfish payloads. A raw
        chassis walk carries serial numbers, part numbers, asset tags, MAC
        addresses and the machine's own inventory of who bought it; a recorded
        capture of one is a fleet inventory disclosure, and the natural way to
        build a realistic test fixture is to commit exactly that. Capturing the
        parsed form keeps names, readings, units, states and thresholds, and
        carries none of the rest -- so the safe thing is also the default thing,
        with no flag to remember.

        A sensor NAME can still embed a hostname on some platforms. Read a
        capture before committing it.
        """
        return {
            "format": WALK_FORMAT,
            "chassis": list(self.chassis),
            "shapes_seen": sorted(self.shapes_seen),
            "errors": [list(e) for e in self.errors],
            "captured_at": self.captured_at,
            "latencies": [[p, round(t, 6)] for p, t in self.latencies],
            "sensors": [
                {"name": s.name, "path": s.path, "reading": s.reading,
                 "units": s.units, "state": s.state, "health": s.health,
                 "shape": s.source_shape,
                 "thresholds": {f"{b}/{lv}": v for (b, lv), v in sorted(s.thresholds.items())}}
                for s in self.sensors
            ],
        }


class RedfishClient:
    """Minimal Redfish reader. GET and JSON, nothing else."""

    def __init__(self, base_url: str, *, username: str | None = None,
                 password: str | None = None, verify_tls: bool = True,
                 timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # (path, seconds) per fetch, in walk order. On the client rather than
        # threaded through every walk function: the walker calls `get` from six
        # places and a parameter would have to reach all of them.
        self.latencies: list[tuple[str, float]] = []
        self._auth: str | None = None
        if username is not None:
            raw = f"{username}:{password or ''}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        self._ctx: ssl.SSLContext | None = None
        if not verify_tls:
            # BMCs ship self-signed certificates as a rule. Opt-in, never default:
            # the flag has to be typed, so nobody disables verification by accident.
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self._auth:
            request.add_header("Authorization", self._auth)
        # One field, taken at the single place every fetch passes through. A BMC
        # whose Redfish stack is degrading answers more slowly long before it
        # answers wrongly, and the walk already touches every endpoint -- so the
        # measurement costs a clock read and nothing else.
        #
        # `perf_counter` rather than wall time: this is an interval, and a wall
        # clock that steps during a walk would record a negative one.
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=self.timeout, context=self._ctx) as response:
            body = response.read()
        # Measured around the read as well as the request. A Redfish collection
        # arrives in one body, and timing only the connection would report a slow
        # BMC as fast.
        self.latencies.append((path, time.perf_counter() - started))
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError(f"{url} returned {type(parsed).__name__}, not an object")
        return parsed


def _members(payload: dict[str, Any]) -> list[str]:
    out = []
    for member in payload.get("Members") or []:
        if isinstance(member, dict) and isinstance(member.get("@odata.id"), str):
            out.append(member["@odata.id"])
    return out


def _link(payload: dict[str, Any], key: str) -> str | None:
    node = payload.get(key)
    if isinstance(node, dict) and isinstance(node.get("@odata.id"), str):
        return node["@odata.id"]
    return None


def read_sensor_object(obj: dict[str, Any], path: str, shape: str = "sensors") -> LiveSensor:
    """Turn one Redfish object into a LiveSensor, from either schema generation.

    `Reading` is taken as absent rather than zero when missing or null. That
    distinction is the entire product: a sensor reporting 0 and a sensor
    reporting nothing are different facts, and collapsing them loses the finding.
    """
    status = obj.get("Status") if isinstance(obj.get("Status"), dict) else {}
    thresholds: dict[tuple[str, str], float] = {}

    nested = obj.get("Thresholds")
    if isinstance(nested, dict):
        for key, slot in _MODERN_THRESHOLDS.items():
            node = nested.get(key)
            if isinstance(node, dict) and isinstance(node.get("Reading"), (int, float)):
                thresholds[slot] = float(node["Reading"])

    for key, slot in _LEGACY_THRESHOLDS.items():
        value = obj.get(key)
        if isinstance(value, (int, float)):
            thresholds.setdefault(slot, float(value))

    reading = obj.get("Reading")
    if reading is None:
        # The deprecated Thermal/Power schema names the value after its kind.
        for key in ("ReadingCelsius", "ReadingVolts", "ReadingRPM", "ReadingWatts",
                    "PowerConsumedWatts"):
            if isinstance(obj.get(key), (int, float)):
                reading = obj[key]
                break

    name = obj.get("Name") or obj.get("MemberId") or path.rsplit("/", 1)[-1]
    return LiveSensor(
        name=str(name), path=path,
        reading=float(reading) if isinstance(reading, (int, float)) else None,
        units=obj.get("ReadingUnits") or obj.get("ReadingType"),
        state=(status.get("State") if isinstance(status.get("State"), str) else None),
        health=(status.get("Health") if isinstance(status.get("Health"), str) else None),
        thresholds=thresholds, source_shape=shape)


def walk_chassis(client: RedfishClient) -> Walk:
    """Enumerate every sensor the target reports, across both tree shapes."""
    walk = Walk()
    # Reset first: a client reused across walks would hand the second walk the
    # first one's timings, which is the shape of measurement bug that reads as a
    # BMC getting slower while nothing changed.
    client.latencies = []

    try:
        collection = client.get("/redfish/v1/Chassis")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        walk.errors.append(("/redfish/v1/Chassis", str(exc)))
        walk.latencies = list(client.latencies)
        return walk

    for chassis_path in _members(collection):
        try:
            chassis = client.get(chassis_path)
        except Exception as exc:  # noqa: BLE001 -- transport variety is the point
            walk.errors.append((chassis_path, str(exc)))
            continue
        walk.chassis.append(chassis_path)

        # Modern shape first, then the deprecated one. Both are read when both
        # links exist: a chassis that carries Thermal AND ThermalSubsystem may
        # expose sensors under only one of them.
        for link_key, shape in (("Sensors", "sensors"),
                                ("Thermal", "thermal"),
                                ("Power", "power")):
            target = _link(chassis, link_key)
            if target is None:
                continue
            walk.shapes_seen.add(shape)
            if shape == "sensors":
                _walk_sensor_collection(client, target, walk)
            else:
                _walk_legacy(client, target, walk, shape)

    _merge_shapes(walk)
    walk.latencies = list(client.latencies)
    # Stamped where the walk is TAKEN, never in `to_dict`. Serialising is not
    # observing: a walk rehydrated from a year-old capture and written back out
    # would otherwise claim to have been taken today, which is the one thing a
    # timestamp exists to prevent.
    walk.captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return walk


def _merge_shapes(walk: Walk) -> None:
    """Collapse the same physical sensor reported by two trees into one.

    A chassis that carries both `Sensors` and `Thermal`/`Power` reports most
    sensors twice, and left alone that produces one match and one
    `undeclared_present` for every sensor on the machine -- a report that is
    entirely noise on a healthy board.

    The modern reading wins where both exist. What is worth keeping is the
    DISAGREEMENT: a sensor the deprecated tree reports and the modern collection
    omits. That is the Redfish form of the case on the OpenBMC list -- a sensor
    visible on one interface and absent from another -- and it is a real finding
    about the firmware, not about the hardware.

    Only that direction is reported. The reverse has no signal in it: `Thermal`
    carries temperatures and fans and `Power` carries voltages, so a sensor
    legitimately absent from a legacy array is absent because of what it
    measures, not because anything is wrong.
    """
    if "sensors" not in walk.shapes_seen:
        return

    modern = {s.name for s in walk.sensors if s.source_shape == "sensors"}
    merged: list[LiveSensor] = [s for s in walk.sensors if s.source_shape == "sensors"]
    for sensor in walk.sensors:
        if sensor.source_shape == "sensors" or sensor.name in modern:
            continue
        merged.append(sensor)
        walk.divergence.append((sensor.name, sensor.source_shape))
    walk.sensors = merged


def _walk_sensor_collection(client: RedfishClient, path: str, walk: Walk) -> None:
    try:
        collection = client.get(path)
    except Exception as exc:  # noqa: BLE001
        walk.errors.append((path, str(exc)))
        return
    for member_path in _members(collection):
        try:
            walk.sensors.append(
                read_sensor_object(client.get(member_path), member_path, "sensors"))
        except Exception as exc:  # noqa: BLE001
            walk.errors.append((member_path, str(exc)))


def _walk_legacy(client: RedfishClient, path: str, walk: Walk, shape: str) -> None:
    """Thermal and Power carry their sensors inline as arrays, not as members."""
    try:
        payload = client.get(path)
    except Exception as exc:  # noqa: BLE001
        walk.errors.append((path, str(exc)))
        return
    arrays = ("Temperatures", "Fans") if shape == "thermal" else ("Voltages", "PowerSupplies")
    for array in arrays:
        for index, obj in enumerate(payload.get(array) or []):
            if isinstance(obj, dict):
                walk.sensors.append(
                    read_sensor_object(obj, f"{path}#/{array}/{index}", shape))

    if shape == "power":
        # `PowerControl` is where the deprecated schema puts chassis draw, and
        # `PowerConsumedWatts` appears nowhere else in it. This walker read only
        # `Voltages` and `PowerSupplies` while the object parser already handled
        # `PowerConsumedWatts`, so the parser carried a branch nothing could reach
        # and chassis power was dropped without a word. Found against a 2021
        # OpenBMC release image; no fixture could show it, because the mock only
        # ever served what this reader was already looking for.
        #
        # **An entry with no measurement is skipped rather than reported.** A
        # `PowerControl` object is a power *limit* control first and a sensor only
        # incidentally; the 2.9.0 machines publish one carrying nothing but a null
        # `LimitInWatts`. Emitting that as a sensor would invent a reading-less
        # sensor from a control knob, and every downstream count -- present,
        # absent, not-reading -- would inherit it.
        for index, obj in enumerate(payload.get("PowerControl") or []):
            if not isinstance(obj, dict):
                continue
            sensor = read_sensor_object(obj, f"{path}#/PowerControl/{index}", shape)
            if sensor.reading is None:
                continue
            walk.sensors.append(sensor)


def order_walks(walks: list[Walk]) -> tuple[list[Walk], str | None]:
    """Put walks in the order they were taken, or say why that could not be done.

    **Chronology was the caller's responsibility and nothing could check it.** The
    feeder's contract is *oldest to newest*: the last report supplies every current
    reading, and every bound verdict is judged against those. A shell glob supplies
    LEXICAL order, in which `walk10.json` sorts before `walk9.json` -- so
    `--walk walks/*.json` over two hundred captures reported walk 99 as the present
    state of the machine. Measured, not hypothesised: the run announced a reading of
    108 where the newest capture said 209.

    That is worse than a stale number. Absence, liveness and every threshold
    comparison are computed against whichever walk landed last in `argv`.

    Returns `(ordered, note)`. The note is for the operator and is never silently
    swallowed -- an input this function could not order is a condition to surface,
    not one to survive.

    **A subset is never sorted.** If some walks carry a timestamp and others do not,
    there is no ordering over the whole set, and inventing one by putting the
    stamped ones in order and leaving the rest where they fell would produce a
    confident sequence that is wrong in an unpredictable place. Mixed input keeps
    the caller's order and says so.
    """
    if len(walks) < 2:
        return walks, None

    stamped = [w for w in walks if w.captured_at]
    if not stamped:
        return walks, (
            f"{len(walks)} walks carry no capture time, so their order is the one "
            f"you supplied and nothing here can check it. A shell glob sorts "
            f"lexically -- `walk10` before `walk9` -- and the LAST walk supplies "
            f"every current reading. Re-capture to record the time, or pass them "
            f"oldest first")
    if len(stamped) != len(walks):
        return walks, (
            f"{len(stamped)} of {len(walks)} walks carry a capture time. A partial "
            f"ordering is not an ordering, so the order you supplied was kept "
            f"unchanged rather than guessed at")

    ordered = sorted(walks, key=lambda w: w.captured_at or "")
    if [id(w) for w in ordered] == [id(w) for w in walks]:
        return ordered, None
    return ordered, (
        f"{len(walks)} walks reordered by capture time; the order supplied was not "
        f"chronological, and the last walk supplies every current reading")


def walk_from_dict(payload: dict[str, Any]) -> Walk:
    """Rehydrate a walk from `Walk.to_dict`, or from a raw Redfish dump.

    Both are accepted because both get produced in practice: this tool writes the
    parsed form, and someone debugging a BMC will have a directory of raw
    responses they want to diff without re-walking. The format marker decides;
    an unmarked payload is read as raw objects.
    """
    walk = Walk()
    walk.errors = [tuple(e) for e in payload.get("errors", ())]
    walk.latencies = [(str(p), float(t))
                      for p, t in payload.get("latencies", ())]
    stamp = payload.get("captured_at")
    walk.captured_at = str(stamp) if stamp else None
    walk.chassis = list(payload.get("chassis", ()))
    walk.shapes_seen = set(payload.get("shapes_seen", ()))

    if payload.get("format") == WALK_FORMAT:
        for item in payload.get("sensors", ()):
            thresholds: dict[tuple[str, str], float] = {}
            for slot, value in (item.get("thresholds") or {}).items():
                bound, _, level = slot.partition("/")
                if level:
                    thresholds[(bound, level)] = float(value)
            walk.sensors.append(LiveSensor(
                name=item["name"], path=item.get("path", item["name"]),
                reading=item.get("reading"), units=item.get("units"),
                state=item.get("state"), health=item.get("health"),
                thresholds=thresholds, source_shape=item.get("shape", "sensors")))
        return walk

    for item in payload.get("sensors", ()):
        walk.sensors.append(read_sensor_object(
            item, item.get("@odata.id") or item.get("path") or str(item.get("Name", "?")),
            item.get("_shape", "sensors")))
    return walk
