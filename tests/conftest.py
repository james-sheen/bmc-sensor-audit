"""The suite is a consumer, so it supplies a vertical like any other.

Nothing in the neutral half classifies without a registered vocabulary -- that
refusal is deliberate and is tested. A test calling those paths directly is a
caller that skipped the command line, so it does here what the command line does
at startup.

Session-scoped and autouse: a per-test registration would let ordering decide
whether a test passes, which is the failure mode a module-level registry invites.
"""

import pytest

from bmc_sensor_audit.verticals import bmc


@pytest.fixture(autouse=True, scope="session")
def _bundled_vertical():
    bmc.register()
