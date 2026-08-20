#!/usr/bin/env python3
"""Derive the set of standard Redfish sensor properties from DMTF's own schemas.

    python3 tools/derive_redfish_properties.py --out src/bmc_sensor_audit/inventory/redfish_properties.json

**Why this is a script and not a list somebody typed.** `--strict-fields` names
the properties a machine reports that the standard does not define, and that
verdict is only as good as its notion of *standard*. A hand-written list of
property names is a vocabulary transcribed from memory: it enumerates what its
author happened to recall, every omission produces a confident false finding
against a firmware doing nothing wrong, and nothing in the tree can tell the two
apart. The property set has exactly one producer -- the published schema -- so it
is read from there, with the version and the hash of what was read recorded
beside it.

**Newest version, on purpose.** DMTF's own compatibility rule is that a minor
version never removes a property; it deprecates. So the newest schema is the
union of every earlier one, and using it is the most permissive choice
available -- which is the right direction for a drift detector, because the cost
of an omission is a false accusation and the cost of an extra name is a signal
this build does not raise.

The annotation pattern is derived too, from the schemas' own `patternProperties`.
Every definition must agree on it or this refuses to write: annotations are how
Redfish carries protocol metadata alongside data, and a build that mistook one
for a vendor extension would report `Reading@Redfish.AllowableValues` as drift on
every machine in the fleet.

Run it again to refresh. The output file records what it read, so a reviewer can
re-fetch those exact URLs and compare hashes without trusting this run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

EXIT_OK, EXIT_FAILED = 0, 1

BASE = "https://redfish.dmtf.org/schemas/v1"
FORMAT = "bmc-sensor-audit/redfish-properties/1"

# The resource types this tool actually parses objects out of, and the schema
# each one is defined in. Not a survey of Redfish: it is the walker's own read
# surface, so a type appearing here means `read_sensor_object` is called on
# objects of that type somewhere in `inventory/redfish.py`.
#
# `Sensor` is the modern collection. The other five are the deprecated
# `Thermal`/`Power` trees, which real fleets still serve -- often at a different
# firmware level on the same SKU.
READ_SURFACE = {
    "Sensor": ("Sensor",),
    "Thermal": ("Temperature", "Fan"),
    "Power": ("Voltage", "PowerSupply", "PowerControl"),
}


def fetch(url: str) -> tuple[bytes, dict]:
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read()
    return raw, json.loads(raw)


def newest_version(schema_name: str) -> str:
    """The newest published version of a schema, from its own version index.

    The unversioned document lists every version as an `anyOf` of `$ref`s in
    release order. Reading the last one asks DMTF what the newest version is
    rather than pinning a number here that goes stale silently.
    """
    _, index = fetch(f"{BASE}/{schema_name}.json")
    refs = [entry.get("$ref", "") for entry in index["definitions"][schema_name]["anyOf"]
            if isinstance(entry, dict)]
    # Fragment first, then path. A `$ref` carries both -- `.../Sensor.v1_13_0.json
    # #/definitions/Sensor` -- and splitting on the separator that comes second
    # returns `Sensor`, which is a real schema name and fetches the unversioned
    # index again. That resolved, fetched, and failed one step later complaining
    # that the schema declares no properties.
    versioned = [ref.split("#", 1)[0].rsplit("/", 1)[-1]
                 for ref in refs if f"{schema_name}.v1_" in ref]
    if not versioned:
        raise SystemExit(f"{schema_name}.json lists no versioned definitions")
    return versioned[-1].removesuffix(".json")


def derive(pins: dict[str, str] | None = None) -> dict:
    pins = pins or {}
    types: dict[str, list[str]] = {}
    sources: list[dict] = []
    patterns: set[str] = set()
    copyrights: set[str] = set()

    for schema_name, definitions in READ_SURFACE.items():
        version = pins.get(schema_name) or newest_version(schema_name)
        url = f"{BASE}/{version}.json"
        raw, document = fetch(url)
        copyrights.add(str(document.get("copyright", "")))
        for definition in definitions:
            block = document["definitions"].get(definition)
            if block is None:
                raise SystemExit(
                    f"{version} does not define {definition!r}; the walker reads "
                    f"objects of that type, so a property set for it is not optional")
            properties = block.get("properties")
            if not properties:
                raise SystemExit(f"{version}#{definition} declares no properties")
            types[definition] = sorted(properties)
            patterns.update(block.get("patternProperties") or {})
        sources.append({
            "schema": version, "url": url,
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "defines": list(definitions),
        })

    if len(patterns) != 1:
        # Never resolved by choosing one. An annotation is protocol metadata and a
        # vendor extension is data; if the schemas stop agreeing on how to tell
        # them apart, this build no longer knows either, and guessing would mean
        # reporting standard annotations as drift on every machine.
        raise SystemExit(
            f"the schemas declare {len(patterns)} different annotation patterns: "
            f"{sorted(patterns)}. Nothing here can pick one")

    return {
        "format": FORMAT,
        "derived_by": "tools/derive_redfish_properties.py",
        "derived_on": date.today().isoformat(),
        "copyright": sorted(copyrights),
        "annotation_pattern": patterns.pop(),
        "sources": sources,
        "types": types,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the data file to write")
    parser.add_argument("--pin", action="append", default=[], metavar="Schema=Version",
                        help="pin one schema, e.g. Sensor=Sensor.v1_13_0, instead of "
                             "taking the newest; repeatable. Use it to reproduce an "
                             "earlier derivation exactly")
    args = parser.parse_args(argv)

    pins = {}
    for item in args.pin:
        name, _, version = item.partition("=")
        if not version:
            print(f"--pin {item!r} is not Schema=Version", file=sys.stderr)
            return EXIT_FAILED
        pins[name] = version

    data = derive(pins)
    Path(args.out).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    total = sum(len(v) for v in data["types"].values())
    print(f"wrote {args.out}")
    for source in data["sources"]:
        print(f"  {source['schema']:22s} {source['sha256'][:16]}  "
              f"{', '.join(source['defines'])}")
    print(f"  {len(data['types'])} resource type(s), {total} property name(s)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
