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
import hashlib
import hmac
import http.client
import json
import ssl
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import redfish_schema

__all__ = ["LiveSensor", "Walk", "RedfishClient", "walk_chassis", "read_sensor_object",
           "walk_from_dict", "order_walks", "validate_walk", "walk_digest",
           "etag_cache", "membership_unchanged", "ETAG_CACHE_FORMAT",
           "CertificatePinError",
           "WALK_FORMAT", "LEGACY_RESOURCES"]

WALK_FORMAT = "bmc-sensor-audit/walk/1"

# Which schema type each deprecated-tree array holds. The arrays are the walker's
# read surface for `Thermal` and `Power`, and every one of them is a different
# resource type with a different property set -- `Fan` declares thirty-two
# properties and `Voltage` nineteen, so judging both against one merged set would
# report standard `Fan` properties as drift on every machine with a fan in it.
LEGACY_RESOURCES = {
    "Temperatures": "Temperature", "Fans": "Fan",
    "Voltages": "Voltage", "PowerSupplies": "PowerSupply",
    "PowerControl": "PowerControl",
}

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
    # Property NAMES this object carried that the published schema for its resource
    # type does not declare. Names only, never values: a Redfish sensor object can
    # carry `SerialNumber` and `PartNumber`, and a report that quoted what it found
    # would publish the machine's identity while complaining about the field.
    undeclared: tuple[str, ...] = ()
    resource: str = "Sensor"             # the Redfish schema type it was read as

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
    # Whether the properties of each sensor object were compared against the schema
    # at walk time. False for a capture written before that existed -- and the
    # distinction is the whole point of the flag. `capture` writes the PARSED sensor
    # set, so an old capture carries no record of which properties the object had;
    # every sensor in it reports no undeclared properties, and a strictness report
    # over one would print a clean board on evidence it does not have. That is the
    # vacuous pass this project keeps finding in other people's systems.
    fields_observed: bool = False

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
            "fields_observed": self.fields_observed,
            "latencies": [[p, round(t, 6)] for p, t in self.latencies],
            "sensors": [
                {"name": s.name, "path": s.path, "reading": s.reading,
                 "units": s.units, "state": s.state, "health": s.health,
                 "shape": s.source_shape, "resource": s.resource,
                 # Written only when there is something to say. The walk-level
                 # `fields_observed` flag is what distinguishes "nothing
                 # undeclared" from "nobody looked", so the per-sensor key does
                 # not have to carry an empty list to stay honest.
                 **({"undeclared": list(s.undeclared)} if s.undeclared else {}),
                 "thresholds": {f"{b}/{lv}": v for (b, lv), v in sorted(s.thresholds.items())}}
                for s in self.sensors
            ],
        }


class CertificatePinError(Exception):
    """The BMC presented a certificate that is not the pinned one."""


def _fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _pinned_opener(pin: str) -> urllib.request.OpenerDirector:
    """An opener that refuses any certificate but the one named.

    The comparison is constant-time and case-insensitive, and it accepts the
    colon-separated spelling that `openssl x509 -fingerprint` prints, because
    that is where an operator copies the value from and re-typing it by hand is
    how a pin ends up subtly wrong.
    """
    expected = pin.replace(":", "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise CertificatePinError(
            f"a SHA-256 pin is 64 hex characters; got {len(expected)}. "
            f"`openssl x509 -in cert.pem -noout -fingerprint -sha256` prints one")

    context = ssl.create_default_context()
    # Off because the pin is the verification. Leaving them on would refuse every
    # self-signed BMC certificate before the fingerprint was ever compared, which
    # is the whole population this flag exists for.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    class _PinnedConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            der = self.sock.getpeercert(binary_form=True)
            got = _fingerprint(der) if der else ""
            if not hmac.compare_digest(got, expected):
                self.close()
                raise CertificatePinError(
                    f"the BMC presented sha256:{got or '(no certificate)'} and "
                    f"the pin is sha256:{expected}")

    class _PinnedHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001,ANN201 - urllib's signature
            return self.do_open(_PinnedConnection, req, context=context)

    return urllib.request.build_opener(_PinnedHandler)


class RedfishClient:
    """Minimal Redfish reader. GET and JSON, nothing else."""

    def __init__(self, base_url: str, *, username: str | None = None,
                 password: str | None = None, verify_tls: bool = True,
                 timeout: float = 15.0, cafile: str | None = None,
                 pin_sha256: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # (path, seconds) per fetch, in walk order. On the client rather than
        # threaded through every walk function: the walker calls `get` from six
        # places and a parameter would have to reach all of them.
        self.latencies: list[tuple[str, float]] = []
        #: `{path: etag}` for every resource that answered with one, and the
        #: subset of those paths whose payload carried a `Members` list. Only
        #: the second set is worth probing later: a COLLECTION's representation
        #: changes when its membership does, which is the question this tool
        #: asks. See `membership_unchanged`.
        self.observed_etags: dict[str, str] = {}
        self.collections: set[str] = set()
        self._auth: str | None = None
        if username is not None:
            raw = f"{username}:{password or ''}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        self._ctx: ssl.SSLContext | None = None
        self._opener: urllib.request.OpenerDirector | None = None
        if (pin_sha256 is not None or cafile is not None) and \
                not self.base_url.lower().startswith("https://"):
            # **A declared expectation must never be met with silence.** urllib
            # picks a handler by SCHEME, so a pinned HTTPS handler is simply not
            # consulted for an `http://` URL: the pin would be built, ignored,
            # and the walk would succeed unverified. An operator who typed a
            # fingerprint would believe the connection was checked.
            #
            # Found by a downstream end-to-end test that pinned a wrong
            # certificate and expected the walk to FAIL. It passed.
            raise CertificatePinError(
                f"{'--pin-sha256' if pin_sha256 else '--cafile'} was given for "
                f"{self.base_url}, which is not https. Nothing would verify the "
                f"connection and the flag would be silently ignored")
        if not verify_tls and (pin_sha256 is not None or cafile is not None):
            # The same reasoning as the refusal above, one door further out.
            # `--insecure` turns verification OFF and these turn it ON, so one
            # of the two was going to be discarded -- and it was discarded
            # SILENTLY, by a precedence nobody typed. An operator who wrote
            # `--insecure --pin-sha256 <fp>` got a verified connection, and one
            # who read the precedence the other way round would have believed
            # the opposite. Neither reading is wrong enough to guess between.
            #
            # Two flags that both VERIFY are a different case and stay legal:
            # `--cafile` with a pin is a fleet CA plus one recorded machine, and
            # `fleet-sensor-baseline` relies on exactly that -- a run-level CA
            # with per-target pins. Precedence is documented there and here.
            raise CertificatePinError(
                f"--insecure was given with "
                f"{'--pin-sha256' if pin_sha256 is not None else '--cafile'}. "
                f"One turns verification off and the other turns it on, so "
                f"whichever this preferred would be a guess at which you meant. "
                f"Drop one")
        if pin_sha256 is not None:
            # **Pinning REPLACES chain verification, it does not add to it.** A
            # BMC's certificate is self-signed and chains to nothing, so there is
            # no path to validate; what can be checked is that the certificate is
            # the exact one the operator recorded. Verifying the fingerprint on
            # every connection is stronger than a trust store for this case and
            # weaker for every other, which is why it is a separate flag.
            self._opener = _pinned_opener(pin_sha256)
        elif cafile is not None:
            # Hostname checking stays ON. A BMC reached by IP whose certificate
            # names a hostname will fail here, and that failure is correct --
            # `--pin-sha256` is the flag for that case, not a quieter `--cafile`.
            self._ctx = ssl.create_default_context(cafile=cafile)
        elif not verify_tls:
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
        opened = (self._opener.open(request, timeout=self.timeout)
                  if self._opener is not None
                  else urllib.request.urlopen(request, timeout=self.timeout,
                                              context=self._ctx))
        with opened as response:
            body = response.read()
            response_headers = response.headers
        # Measured around the read as well as the request. A Redfish collection
        # arrives in one body, and timing only the connection would report a slow
        # BMC as fast.
        self.latencies.append((path, time.perf_counter() - started))
        etag = response_headers.get("ETag")
        if etag:
            self.observed_etags[path] = etag
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError(f"{url} returned {type(parsed).__name__}, not an object")
        if isinstance(parsed.get("Members"), list):
            self.collections.add(path)
        return parsed

    def probe_unchanged(self, path: str, etag: str) -> bool | None:
        """Conditional GET. True unchanged, False changed, None cannot tell.

        **The returned ETag is compared, not just the status.** A BMC that
        ignores `If-None-Match` answers 200 and hands back the same ETag it gave
        last time -- reading that as *changed* would make the whole mechanism
        useless on exactly the firmware it was meant to help. A 200 with no ETag
        at all is *cannot tell*, which is neither.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "If-None-Match": etag})
        if self._auth:
            request.add_header("Authorization", self._auth)
        try:
            opened = (self._opener.open(request, timeout=self.timeout)
                      if self._opener is not None
                      else urllib.request.urlopen(request, timeout=self.timeout,
                                                  context=self._ctx))
            with opened as response:
                response.read()
                fresh = response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return True
            raise
        if not fresh:
            return None
        self.observed_etags[path] = fresh
        return fresh == etag


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


def read_sensor_object(obj: dict[str, Any], path: str, shape: str = "sensors",
                       resource: str = "Sensor") -> LiveSensor:
    """Turn one Redfish object into a LiveSensor, from either schema generation.

    `Reading` is taken as absent rather than zero when missing or null. That
    distinction is the entire product: a sensor reporting 0 and a sensor
    reporting nothing are different facts, and collapsing them loses the finding.

    `resource` names the Redfish schema type the object was read as, which decides
    which property set it is judged against. `shape` says which TREE it came from
    and cannot answer that: `thermal` covers both `Temperature` and `Fan`, and
    those two schemas declare different properties.
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
        thresholds=thresholds, source_shape=shape, resource=resource,
        undeclared=redfish_schema.undeclared_properties(obj, resource))


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
    # Set here and nowhere else: every object this walk parsed went through
    # `read_sensor_object`, which compares against the schema unconditionally. The
    # flag records that the comparison HAPPENED, so an empty result downstream
    # means the machine carried nothing undeclared rather than that nobody looked.
    walk.fields_observed = True
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
                read_sensor_object(client.get(member_path), member_path, "sensors",
                                   "Sensor"))
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
                    read_sensor_object(obj, f"{path}#/{array}/{index}", shape,
                                       LEGACY_RESOURCES[array]))

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
            sensor = read_sensor_object(obj, f"{path}#/PowerControl/{index}", shape,
                                        LEGACY_RESOURCES["PowerControl"])
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
        # Absent means the capture predates field observation, so it stays False.
        # Defaulting it to True would make every capture ever written claim its
        # sensors carried no undeclared properties, on no evidence at all.
        walk.fields_observed = bool(payload.get("fields_observed", False))
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
                thresholds=thresholds, source_shape=item.get("shape", "sensors"),
                resource=str(item.get("resource", "Sensor")),
                undeclared=tuple(item.get("undeclared") or ())))
        return walk

    # A raw dump still carries the objects themselves, so the comparison can be
    # made now rather than at walk time. `_shape` and `_resource` are this
    # project's own markers on a hand-assembled fixture; a genuine Redfish dump
    # carries neither, and `Sensor` is the collection everything modern lives in.
    walk.fields_observed = True
    for item in payload.get("sensors", ()):
        # The markers are removed before the object is judged. Leaving them in
        # would make every hand-assembled fixture report `_shape` as a property
        # the standard does not declare -- a finding this project invented and
        # then found.
        obj = {k: v for k, v in item.items() if not k.startswith("_")}
        walk.sensors.append(read_sensor_object(
            obj, item.get("@odata.id") or item.get("path") or str(item.get("Name", "?")),
            item.get("_shape", "sensors"), item.get("_resource", "Sensor")))
    return walk


# Every `bound/level` slot this writer can produce, DERIVED from the two threshold
# maps rather than listed again here. A transcribed vocabulary is one that drifts
# from the thing it describes the first time a mapping is added, and the validator
# would then refuse a slot this build had just written.
_THRESHOLD_SLOTS = frozenset(_MODERN_THRESHOLDS.values()) | frozenset(
    _LEGACY_THRESHOLDS.values())


def validate_walk(payload: Any) -> list[str]:
    """Everything wrong with this walk file, or an empty list.

    The mirror of `validate_attestation`, and it ships for the same reason that one
    does: **the person who RECEIVES the file is the one who needs to check it.** A
    collector ingesting thousands of captures has to be able to refuse a malformed
    one using the format's own words, not a shape it inferred from the files that
    happened to arrive first.

    **Malformation only.** A walk that carries no sensors and no errors is a legal
    capture of a chassis that reports no sensors, and refusing it here would fail a
    file this tool writes -- a validator that rejects valid input is one people learn
    to route around, taking the malformed cases with it. What is suspicious rather
    than wrong is printed by the caller and never scored.

    **Permissive where the reader is permissive.** `captured_at`, `latencies` and
    `fields_observed` are all absent from captures written before those fields
    existed, and `walk_from_dict` reads such a file correctly. Absent is therefore
    accepted; present-and-the-wrong-type is not, because that is the case where the
    reader takes a value it cannot use.

    Returns problems rather than raising, so a caller reports all of them at once
    instead of one per run.

    **Imports nothing outside the standard library**, and in particular no engine: a
    walk is JSON and checking one is a Stage 1 operation.
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return [f"the walk is {type(payload).__name__}, not an object"]

    declared = payload.get("format")
    if declared != WALK_FORMAT:
        problems.append(f"format is {declared!r}, this build reads {WALK_FORMAT!r}")

    if not isinstance(payload.get("sensors"), list):
        # Everything below iterates it, so reporting a type error and then every
        # consequence of it names one fault many times over. Same split as the
        # attestation validator, and for the same reason.
        return problems + ["'sensors' is missing or is not a list"]

    for key in ("chassis", "shapes_seen", "errors", "latencies"):
        if key in payload and not isinstance(payload[key], list):
            problems.append(f"{key!r} is present and is not a list")
    if payload.get("captured_at") is not None and not isinstance(
            payload["captured_at"], str):
        problems.append("'captured_at' is present and is not a string")
    if "fields_observed" in payload and not isinstance(
            payload["fields_observed"], bool):
        problems.append("'fields_observed' is present and is not a boolean")

    observed = bool(payload.get("fields_observed", False))
    for index, item in enumerate(payload["sensors"]):
        where = f"sensors[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} is {type(item).__name__}, not an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            # The reader indexes this key directly, so a capture without it raises
            # rather than degrading. Naming it here is what turns an unreadable file
            # into a reported one.
            problems.append(f"{where} carries no 'name'; every other field on a "
                            f"sensor is optional and this one is the identity")
            continue
        where = f"{where} ({name})"
        if not _is_number_or_absent(item.get("reading")):
            problems.append(f"{where} has a non-numeric 'reading'")
        problems.extend(_threshold_problems(item, where))
        undeclared = item.get("undeclared")
        if undeclared is not None:
            if not isinstance(undeclared, list) or not all(
                    isinstance(n, str) for n in undeclared):
                problems.append(f"{where} has an 'undeclared' that is not a list of "
                                f"property names")
            elif undeclared and not observed:
                # The contradiction worth catching, and the only cross-field rule
                # here. `fields_observed` false says nobody compared this object
                # against the schema; an `undeclared` list says somebody did and
                # found something. A reader trusting the flag would report a clean
                # board while the file in front of it names the drift.
                problems.append(
                    f"{where} names undeclared properties while 'fields_observed' "
                    f"is false; the walk says nobody looked and the sensor says "
                    f"somebody did")
    return problems


def _is_number_or_absent(value: Any) -> bool:
    """`bool` is a subclass of `int`, so it passes an `isinstance` number test.

    A capture carrying `"reading": true` would otherwise validate and then rehydrate
    into a sensor reading 1.0 -- a number nothing measured, in the one field the
    whole tool is pointed at.
    """
    if value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _threshold_problems(item: dict, where: str) -> list[str]:
    thresholds = item.get("thresholds")
    if thresholds is None:
        return []
    if not isinstance(thresholds, dict):
        return [f"{where} has a 'thresholds' that is not an object"]
    problems: list[str] = []
    for slot, value in thresholds.items():
        bound, _, level = str(slot).partition("/")
        if (bound, level) not in _THRESHOLD_SLOTS:
            problems.append(
                f"{where} carries a threshold slot {slot!r}, which this build does "
                f"not write. A slot it cannot name is one it cannot compare")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{where} threshold {slot!r} is not a number")
    return problems


ETAG_CACHE_FORMAT = "bmc-sensor-audit/etag-cache/1"


def etag_cache(client: "RedfishClient") -> dict[str, Any]:
    """What to probe on the next visit, recorded from the walk just done.

    **Collections only, and that is the whole design.** A per-resource ETag
    cache would let a walk skip transferring an unchanged sensor -- and to use a
    `304` it would have to have kept the previous BODY, which means a cache of
    raw Redfish payloads on disk. Those carry serial numbers, asset tags and MAC
    addresses; *the parse is the redaction* exists precisely so this tool never
    writes one. So the cache holds ETags and nothing else, and the only question
    ETags alone can answer is about the resources whose representation IS a list
    of members.
    """
    return {
        "format": ETAG_CACHE_FORMAT,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collections": {path: client.observed_etags[path]
                        for path in sorted(client.collections)
                        if path in client.observed_etags},
    }


def membership_unchanged(client: "RedfishClient",
                         cache: dict[str, Any]) -> tuple[bool | None, str]:
    """`(verdict, sentence)` -- is the sensor SET the same as when cached?

    **This is a narrower question than *has anything changed*, and the caller
    must not widen it.** A Redfish collection's representation is its member
    list, so its ETag moves when a sensor appears or disappears. A threshold
    edited on a sensor that stayed present changes the SENSOR resource and not
    the collection, so this answers `True` while a configuration audit would
    have findings.

    Presence is what the collector upstream of this needed, so presence is what
    is offered, under a name that says so.

    `None` means the BMC does not do ETags, or stopped. That is not `True`.
    """
    collections = cache.get("collections") or {}
    if not collections:
        return None, "the cache records no collection ETags, so there is nothing to compare"

    unknown = 0
    for path, etag in sorted(collections.items()):
        try:
            verdict = client.probe_unchanged(path, etag)
        except urllib.error.HTTPError as error:
            return None, f"{path} answered {error.code}; the tree may have moved"
        except OSError as error:
            return None, f"{path} could not be reached: {error}"
        if verdict is False:
            return False, f"{path} changed"
        if verdict is None:
            unknown += 1
    if unknown:
        # Partial support is not support. Answering `unchanged` because the
        # collections that DO carry ETags agreed would be a guess about the ones
        # that do not, made on the machine where it is least checkable.
        return None, (f"{unknown} of {len(collections)} collection(s) returned no "
                      f"ETag, so this BMC cannot answer the question")
    return True, f"all {len(collections)} collection(s) unchanged"


def walk_digest(raw: bytes | str) -> str:
    """A content handle for one capture: `sha256:` and the hex digest of the FILE.

    **Over the bytes, not over a re-serialisation of them.** A canonical-JSON digest
    would survive re-indentation, and it would also require every consumer to
    reproduce one language's float formatting exactly before it could agree with
    this one. The bytes are what the collector received, `sha256sum` computes the
    same value in any language and on any machine, and a recipient can check the
    handle without trusting -- or installing -- this tool at all.

    The cost is stated rather than hidden: rewriting the file changes the handle,
    even where the walk is unchanged. That is correct for a handle on a received
    artifact and wrong for one on a walk's meaning, and this is the first.

    **This is deliberately not identity.** It says which capture, never which
    machine. Binding a capture to a unit happens outside, in the layer whose job is
    to name things: the collector holds `{unit_key, digest, walk_ref}` and this tool
    never sees `unit_key`. No identity field enters `walk/1`.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()
