"""What a BMC declares, and what one reports when walked.

The DIFFERENCE between those two is no longer here: pairing a declaration
against a capture is a question about presence rather than about BMCs, and it
lives in `presence-audit` along with the regression gate, the report, the model
generator and the attestation. What stays is everything that only means
something on a baseboard management controller -- the entity-manager reader, the
Redfish client and its property schema, the sensor taxonomy, and the layered
declaration sources.
"""
