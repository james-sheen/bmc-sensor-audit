"""Declared names carrying a runtime substitution, and how they match.

`$VARIABLE` is entity-manager declaration syntax. The neutral pairing asks
whether a declared name matches a live one; how a domain's templates expand is
the domain's business, and this one has a hard-won lesson in it -- see the
docstring below on the greedy class that once matched every sensor on a machine.
"""

from __future__ import annotations

import re

from ..inventory.entity_manager import ANY_TEMPLATE, KNOWN_TEMPLATE


def template_pattern(name: str) -> re.Pattern[str] | None:
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
    if not ANY_TEMPLATE.search(name):
        return None
    parts = [re.escape(p) for p in KNOWN_TEMPLATE.split(name)]
    if len(parts) == 1:                       # a `$` that named no known variable
        return None
    pattern = r"[\w.:-]+".join(parts)
    if ANY_TEMPLATE.search(pattern):         # a known variable AND an unknown one
        return None
    return re.compile("^" + pattern + "$", re.I)
