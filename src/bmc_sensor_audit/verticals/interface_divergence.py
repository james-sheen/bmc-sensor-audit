"""A sensor the deprecated tree reports and the modern collection omits.

Present on one interface, absent from another -- a firmware defect, and one a
tool reading only its preferred tree cannot see at all.

Moved out of the diff. Every word of the finding it produces is Redfish: a
deprecated tree, a Sensors collection, a chassis, a current schema. The other
bridge built against the same guide has no `divergence` on its capture and no
second interface to diverge from.
"""

from __future__ import annotations

from typing import Sequence

from presence_audit.diff import Finding
from ..inventory.redfish import Walk


def interface_divergence_findings(walk: Walk) -> Sequence[Finding]:
    return [
        Finding(
            "interface_divergence", name,
            f"reported under the deprecated {shape} tree and absent from the "
            f"Sensors collection on the same chassis; a client reading only the "
            f"current schema does not see this sensor at all",
            None, None)
        for name, shape in walk.divergence
    ]
