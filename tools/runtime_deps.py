#!/usr/bin/env python3
"""Print this project's RUNTIME dependencies, one per line.

CI deliberately does not install this package: a test asserting that a bare
module path fails without `PYTHONPATH` has to stay a real negative, and an
installed package makes it vacuous. That was free while this project had no
dependencies at all. It stopped being free when the domain-neutral half moved
to `presence-audit`, because the vertical imports it at module scope -- the
whole suite then failed to collect on a runner, having passed locally, where
an editable install had quietly supplied it.

So the runtime dependencies are installed and the package is not, and the list
comes from `pyproject.toml` rather than from a second copy in a workflow file.
A hand-kept copy is a version range that drifts from the one that binds, and
the drift is invisible until the day the two disagree about something.

`tomllib` is 3.11+, and the version matrix starts at 3.10, so the fallback is
not laziness -- it is the older interpreter this has to run on.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def runtime_dependencies(text: str) -> list[str]:
    """The `[project] dependencies` array, in declaration order.

    Deliberately NOT the optional-dependency groups: an extra is by definition
    something the package runs without, and installing one here would hide a
    missing guard rather than reveal it.
    """
    try:
        import tomllib
        return list(tomllib.loads(text).get("project", {}).get("dependencies", []))
    except ModuleNotFoundError:
        pass
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if block is None:
        return []
    return re.findall(r'"([^"]+)"', block.group(1))


def main() -> int:
    deps = runtime_dependencies((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for dep in deps:
        print(dep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
