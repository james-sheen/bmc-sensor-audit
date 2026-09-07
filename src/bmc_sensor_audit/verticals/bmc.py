"""The BMC vertical: what a baseboard management controller declares.

Everything domain-specific about classifying a declared sensor lives behind
`register()`. The core asks the registry; it never imports this module.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from ..core import vocabulary as _vocabulary
from . import (field_strictness, interface_divergence, name_templates,
               peer_groups, regression_rules)
from ..inventory import sensor_types


class BmcVocabulary:
    """`sensor_types`, presented as the core's vocabulary.

    The count keys are the two the shipped report has always carried. They are
    named here, in the vertical, because they are domain words: a report saying
    *not sensors* is saying something only a BMC audit means.
    """

    @property
    def kinds(self) -> Sequence[str]:
        return sensor_types.KINDS

    @property
    def count_keys(self) -> Mapping[str, str]:
        return {sensor_types.NOT_A_SENSOR: "not_a_sensor",
                sensor_types.UNRECOGNISED: "unrecognised_type"}

    def classify(self, declared_type: Optional[str]) -> str:
        return sensor_types.classify(declared_type)

    def is_auditable(self, kind: str) -> bool:
        return kind == sensor_types.SENSOR

    def is_expected_live(self, declared_type: Optional[str]) -> bool:
        return sensor_types.is_expected_live(declared_type)

    def template_pattern(self, declared_name: str) -> object:
        return name_templates.template_pattern(declared_name)

    def same_point(self, old: object, new: object) -> bool:
        return regression_rules.same_point(old, new)

    def captures_comparable(self, before: object, after: object) -> bool:
        return regression_rules.captures_comparable(before, after)

    def point_changes(self, old: object, new: object, *,
                      comparable: bool = False) -> Sequence[object]:
        return regression_rules.point_changes(old, new, comparable=comparable)

    def capture_changes(self, before: object, after: object) -> Sequence[object]:
        return regression_rules.capture_changes(before, after)

    def capture_findings(self, capture: object) -> Sequence[object]:
        return interface_divergence.interface_divergence_findings(capture)

    def peer_groups(self, declaration: object) -> Sequence[Mapping[str, object]]:
        return peer_groups.pairing_candidates(declaration)

    def report_sections(self) -> Mapping[str, object]:
        return {"strict_fields": field_strictness.strict_fields_payload}


def register() -> str:
    _vocabulary.register(BmcVocabulary())
    return "bmc: sensor kinds and their report keys"
