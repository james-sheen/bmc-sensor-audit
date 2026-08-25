"""A Redfish target that exists only in this process.

It ships with the package rather than living in `tests/` on purpose. Stage 3's
deliverable is a README that shows one platform end to end -- source config,
findings, and three injected faults -- **reproducible with no hardware**, and
that promise is only real if the reader has the mock too.

It serves over real HTTP, through `http.server`, rather than faking the client.
A fake client tests the walker and skips the transport, and the transport is
where a walk fails in the field: a 500 on one chassis, a subtree that times out,
a BMC that closes the connection halfway through an enumeration. Those are the
cases the diff has to distinguish from absence, so they have to be testable.

Both tree shapes are here. `Thermal` and `Power` are deprecated in favour of
`Sensors`, and fleets run both -- frequently at different firmware levels on the
same SKU, which is exactly the population this tool is aimed at.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

__all__ = ["MockSensor", "MockBMC", "serve"]


@dataclass
class MockSensor:
    name: str
    reading: float | None = 20.0
    units: str = "Cel"
    state: str = "Enabled"
    health: str = "OK"
    upper_critical: float | None = None
    upper_warning: float | None = None
    lower_critical: float | None = None
    lower_warning: float | None = None
    # Extra properties this sensor reports, merged into whichever shape is served.
    # A firmware that invents properties is the thing `--strict-fields` exists to
    # name, and it cannot be tested against a mock that can only serve the
    # standard set -- the check would pass on every fixture and be untested.
    extra: dict[str, Any] = field(default_factory=dict)

    def as_modern(self, member_id: str) -> dict[str, Any]:
        """The current schema: thresholds nested, each with its own Reading."""
        thresholds: dict[str, Any] = {}
        for key, value in (("UpperCritical", self.upper_critical),
                           ("UpperCaution", self.upper_warning),
                           ("LowerCritical", self.lower_critical),
                           ("LowerCaution", self.lower_warning)):
            if value is not None:
                thresholds[key] = {"Reading": value, "Activation": "Increasing"}
        obj: dict[str, Any] = {
            "@odata.id": f"/redfish/v1/Chassis/1/Sensors/{member_id}",
            "Id": member_id, "Name": self.name,
            "ReadingType": self.units,
            "Status": {"State": self.state, "Health": self.health},
        }
        if self.reading is not None:
            obj["Reading"] = self.reading
        if thresholds:
            obj["Thresholds"] = thresholds
        obj.update(self.extra)
        return obj

    def as_legacy(self) -> dict[str, Any]:
        """The deprecated schema: thresholds as flat siblings of the reading."""
        obj: dict[str, Any] = {
            "MemberId": self.name, "Name": self.name,
            "ReadingUnits": self.units,
            "Status": {"State": self.state, "Health": self.health},
        }
        if self.reading is not None:
            obj["Reading"] = self.reading
        for key, value in (("UpperThresholdCritical", self.upper_critical),
                           ("UpperThresholdNonCritical", self.upper_warning),
                           ("LowerThresholdCritical", self.lower_critical),
                           ("LowerThresholdNonCritical", self.lower_warning)):
            if value is not None:
                obj[key] = value
        obj.update(self.extra)
        return obj


@dataclass
class MockBMC:
    """A machine that reports a given set of sensors, in a given tree shape.

    `fail` maps a path to an HTTP status, so a subtree can be made to fail
    without taking the whole target down. That is the case worth testing: a
    partial walk is the one a naive tool renders as a chassis full of missing
    sensors.
    """

    sensors: list[MockSensor] = field(default_factory=list)
    shape: str = "sensors"                       # "sensors" | "legacy" | "both"
    fail: dict[str, int] = field(default_factory=dict)
    #: Serve ETags and honour `If-None-Match`. Off by default: a BMC that does
    #: not do ETags is the common case, and it is the one a caller can get
    #: wrong by reading *no answer* as *unchanged*.
    etags: bool = False

    def add(self, name: str, **kwargs: Any) -> MockSensor:
        sensor = MockSensor(name=name, **kwargs)
        self.sensors.append(sensor)
        return sensor

    def disable(self, name: str) -> None:
        """Switch a sensor off, the way a factory setting or a firmware default
        does. It stays in the tree and stops reading."""
        for sensor in self.sensors:
            if sensor.name == name:
                sensor.state, sensor.reading = "Disabled", None

    def remove(self, name: str) -> None:
        """Make a sensor vanish entirely -- the firmware-upgrade case."""
        self.sensors = [s for s in self.sensors if s.name != name]

    def routes(self) -> dict[str, dict[str, Any]]:
        chassis: dict[str, Any] = {
            "@odata.id": "/redfish/v1/Chassis/1", "Id": "1", "Name": "Chassis",
        }
        routes: dict[str, dict[str, Any]] = {
            "/redfish/v1": {"@odata.id": "/redfish/v1",
                            "Chassis": {"@odata.id": "/redfish/v1/Chassis"}},
            "/redfish/v1/Chassis": {
                "@odata.id": "/redfish/v1/Chassis", "Members@odata.count": 1,
                "Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        }

        if self.shape in ("sensors", "both"):
            chassis["Sensors"] = {"@odata.id": "/redfish/v1/Chassis/1/Sensors"}
            members = []
            for index, sensor in enumerate(self.sensors):
                member_id = f"s{index}"
                path = f"/redfish/v1/Chassis/1/Sensors/{member_id}"
                routes[path] = sensor.as_modern(member_id)
                members.append({"@odata.id": path})
            routes["/redfish/v1/Chassis/1/Sensors"] = {
                "@odata.id": "/redfish/v1/Chassis/1/Sensors",
                "Members@odata.count": len(members), "Members": members}

        if self.shape in ("legacy", "both"):
            chassis["Thermal"] = {"@odata.id": "/redfish/v1/Chassis/1/Thermal"}
            chassis["Power"] = {"@odata.id": "/redfish/v1/Chassis/1/Power"}
            # Fans by unit, everything else a temperature. Crude, and it matches
            # how the deprecated schema actually splits its arrays.
            fans = [s.as_legacy() for s in self.sensors if s.units == "RPM"]
            temps = [s.as_legacy() for s in self.sensors if s.units not in ("RPM", "V")]
            volts = [s.as_legacy() for s in self.sensors if s.units == "V"]
            routes["/redfish/v1/Chassis/1/Thermal"] = {
                "@odata.id": "/redfish/v1/Chassis/1/Thermal",
                "Temperatures": temps, "Fans": fans}
            routes["/redfish/v1/Chassis/1/Power"] = {
                "@odata.id": "/redfish/v1/Chassis/1/Power", "Voltages": volts}

        routes["/redfish/v1/Chassis/1"] = chassis
        return routes


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, dict[str, Any]] = {}
    failures: dict[str, int] = {}
    #: Whether this machine implements ETags at all. Off by default, because
    #: plenty of real BMCs do not, and a mock that always did would make the
    #: *cannot tell* path untestable -- which is the path the caller most needs
    #: to get right.
    etags: bool = False

    def do_GET(self) -> None:  # noqa: N802 -- the base class names it
        path = self.path.split("?", 1)[0].rstrip("/") or "/redfish/v1"
        status = self.failures.get(path)
        if status is not None:
            self.send_error(status, "injected failure")
            return
        payload = self.routes.get(path)
        if payload is None:
            self.send_error(404, "no such resource")
            return
        body = json.dumps(payload).encode()
        tag = None
        if self.etags:
            # Derived from the bytes, the way a real implementation derives it
            # from the representation: serve different content, get a different
            # ETag, with nothing to keep in sync by hand.
            tag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if tag:
            self.send_header("ETag", tag)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence. The default handler writes every request to stderr, which
        turns a passing test run into a wall of noise."""


@contextlib.contextmanager
def serve(bmc: MockBMC, *, host: str = "127.0.0.1") -> Iterator[str]:
    """Run `bmc` on an ephemeral port; yield its base URL.

    Binds port 0 and reads back what the OS assigned, so parallel test runs do
    not collide on a hardcoded port.
    """
    handler = type("_Bound", (_Handler,),
                   {"routes": bmc.routes(), "failures": dict(bmc.fail),
                    "etags": bool(getattr(bmc, "etags", False))})
    server = ThreadingHTTPServer((host, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
