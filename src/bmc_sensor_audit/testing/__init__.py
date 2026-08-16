"""A Redfish target that exists only in this process, for tests and demos."""

from .mock_redfish import MockBMC, MockSensor, serve

__all__ = ["MockBMC", "MockSensor", "serve"]
