"""What the published Redfish schema declares, so the walker can say what it does not.

A firmware that adds properties to its sensor objects is drifting away from what
downstream monitoring parses. The drift is invisible to this tool's other checks
by construction: coverage compares names and thresholds, and liveness compares
readings, so a machine can grow an entire vendor dialect without either noticing.
The walker already fetches every one of those objects, so the observation costs a
set difference at a place the bytes are already in hand.

**The property set is derived, never typed.** `redfish_properties.json` is written
by `tools/derive_redfish_properties.py` out of DMTF's own published schemas, and
records the version, URL and SHA-256 of each document it read. A hand-written list
would be a vocabulary recalled rather than derived: every name its author forgot
becomes a confident accusation against firmware doing nothing wrong, and nothing
in the tree could tell that apart from a real finding.

**Annotations are not extensions.** Redfish carries protocol metadata in property
names like `Reading@Redfish.AllowableValues` and `Members@odata.count`. The
pattern that recognises them is itself taken from the schemas' own
`patternProperties`, and the deriving tool refuses to write the file if the six
definitions stop agreeing on it.

**`Oem` is standard.** It is a property every Redfish schema defines, and its
contents are outside the schema BY DESIGN -- that is what the extension point is
for. So a vendor block under `Oem` is not drift, and this never descends into one.
An invented property sitting as a sibling of `Reading` is a different act: it is
an extension made where the standard provided a place not to make one, and that
is exactly the signal worth reporting.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["undeclared_properties", "resource_types", "sources", "annotation_pattern",
           "UnknownResourceType", "DATA_FILE", "FORMAT"]

FORMAT = "bmc-sensor-audit/redfish-properties/1"
DATA_FILE = Path(__file__).with_name("redfish_properties.json")


class UnknownResourceType(KeyError):
    """Asked about a resource type the derived data does not cover.

    Raised rather than answered with an empty set. An empty standard set would
    make every property on the object undeclared, and a strictness report that
    names all sixteen properties of a healthy `PowerControl` entry is worse than
    one that does not run: it looks like a finding.
    """


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT:
        raise ValueError(
            f"{DATA_FILE.name} declares format {payload.get('format')!r}, "
            f"this build reads {FORMAT!r}")
    return payload


@lru_cache(maxsize=1)
def _annotation() -> re.Pattern[str]:
    return re.compile(_data()["annotation_pattern"])


def annotation_pattern() -> str:
    """The pattern, as the schemas wrote it. For the report's provenance line."""
    return _data()["annotation_pattern"]


def resource_types() -> dict[str, tuple[str, ...]]:
    """Every resource type covered, and the properties its schema declares."""
    return {name: tuple(props) for name, props in _data()["types"].items()}


def sources() -> list[dict[str, Any]]:
    """The schema documents the property set was read out of, with their hashes.

    Carried into the report rather than kept for the tests. A reader asked to act
    on `this property is not standard` should be able to see which version of the
    standard said so, and re-fetch it.
    """
    return [dict(entry) for entry in _data()["sources"]]


def undeclared_properties(obj: dict[str, Any], resource: str) -> tuple[str, ...]:
    """Property names on this object that the schema for `resource` does not define.

    Annotations are excluded because they are protocol metadata rather than data.
    `Oem` is excluded because the schema declares it -- see the module docstring.

    Only the top level of the object is examined. Descending would require the
    definition of every nested type as well, and the shallow answer is the one the
    product needs: a sibling of `Reading` that nothing standard defines is what a
    downstream parser trips over.
    """
    known = _data()["types"].get(resource)
    if known is None:
        raise UnknownResourceType(
            f"no derived property set for Redfish resource type {resource!r}; "
            f"covered types are {sorted(_data()['types'])}")
    standard = set(known)
    annotation = _annotation()
    return tuple(sorted(name for name in obj
                        if name not in standard and not annotation.match(name)))
