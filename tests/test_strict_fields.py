"""Field strictness: naming properties the published schema does not declare.

**The test that carries this file is `test_an_old_capture_is_refused_not_passed`.**
Every other check here can be satisfied by a report that never runs. `capture`
writes the PARSED sensor set, so a capture taken before walks recorded object
properties carries no evidence about any property at all -- and a strictness
report over one would print *nothing undeclared* on a board it never looked at.
That is a vacuous pass, and it is the failure mode this project keeps finding in
other people's gates.

The property vocabulary is derived, not typed -- see
`tools/derive_redfish_properties.py`. So the second load-bearing test is
`test_every_property_the_walker_reads_is_one_the_schema_declares`: it points the
derived data back at the reader and asks whether the two agree. If the walker
parses a key no schema defines, either the reader is reading something that does
not exist or the derivation missed a type, and both are worth failing over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_sensor_audit.inventory import redfish_schema
from bmc_sensor_audit.inventory.redfish import (LEGACY_RESOURCES, RedfishClient,
                                                walk_chassis, walk_from_dict)
from bmc_sensor_audit.report import strict_fields_as_text, strict_fields_payload
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve

FIXTURES = Path(__file__).parent / "fixtures"


class TestTheVocabularyIsDerived:
    def test_the_data_file_records_what_it_was_read_from(self):
        sources = redfish_schema.sources()
        assert sources, "the derived data names no source schema"
        for source in sources:
            assert source["url"].startswith("https://redfish.dmtf.org/")
            assert len(source["sha256"]) == 64
            assert source["bytes"] > 0
            assert source["defines"]

    def test_every_resource_type_the_walker_reads_is_covered(self):
        """The deprecated arrays and the modern collection, all six.

        A type the walker parses and the data does not cover raises rather than
        answering, so this failing means `--strict-fields` cannot run at all on a
        machine serving that tree -- not that it runs and says nothing.
        """
        covered = set(redfish_schema.resource_types())
        assert set(LEGACY_RESOURCES.values()) <= covered
        assert "Sensor" in covered

    def test_every_property_the_walker_reads_is_one_the_schema_declares(self):
        """Point the derived set back at the reader.

        These are the keys `read_sensor_object` looks for by name. Every one of
        them must be a property some covered schema declares -- otherwise the
        reader is looking for something that does not exist in the standard, or
        the derivation is missing the type that defines it.
        """
        read_by_the_walker = {
            "Sensor": {"Reading", "ReadingType", "ReadingUnits", "Status",
                       "Thresholds", "Name", "Id"},
            "Temperature": {"ReadingCelsius", "Status", "Name", "MemberId",
                            "UpperThresholdCritical", "UpperThresholdNonCritical",
                            "UpperThresholdFatal", "LowerThresholdCritical",
                            "LowerThresholdNonCritical", "LowerThresholdFatal"},
            "Fan": {"Reading", "ReadingUnits", "Status", "Name", "MemberId",
                    "UpperThresholdCritical", "LowerThresholdCritical"},
            "Voltage": {"ReadingVolts", "Status", "Name", "MemberId",
                        "UpperThresholdCritical", "LowerThresholdCritical"},
            "PowerSupply": {"Status", "Name", "MemberId"},
            "PowerControl": {"PowerConsumedWatts", "Status", "Name", "MemberId"},
        }
        declared = redfish_schema.resource_types()
        for resource, keys in read_by_the_walker.items():
            missing = sorted(keys - set(declared[resource]))
            assert not missing, (
                f"the walker reads {missing} on a {resource}, and no covered "
                f"schema declares them")

    def test_the_data_file_is_inside_the_package_so_it_ships(self):
        """It is product data, not a fixture, and a wheel that omitted it would
        raise on the first `--strict-fields` run rather than at install.

        Verified against a built wheel by hand; pinned here because the thing that
        makes it ship is where it SITS. Moved to a top-level `data/` directory it
        would keep working from a checkout and from an editable install -- both of
        which resolve `__file__` back to this tree -- and fail only for someone who
        installed the package properly.
        """
        import bmc_sensor_audit

        package = Path(bmc_sensor_audit.__file__).parent
        assert redfish_schema.DATA_FILE.is_relative_to(package)
        assert redfish_schema.DATA_FILE.is_file()

        pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        assert 'packages = ["src/bmc_sensor_audit"]' in pyproject, (
            "the wheel no longer packages the directory this data file lives in")

    def test_an_uncovered_resource_type_raises_rather_than_answering(self):
        """Empty would be the dangerous answer.

        With no property set, every property on the object is undeclared, and the
        report would name all sixteen properties of a healthy `PowerControl` entry
        as drift. Refusing to judge is the only safe response to not knowing.
        """
        with pytest.raises(redfish_schema.UnknownResourceType):
            redfish_schema.undeclared_properties({"Name": "x"}, "Thermostat")


class TestWhatCountsAsUndeclared:
    def test_an_invented_property_is_named(self):
        found = redfish_schema.undeclared_properties(
            {"Name": "Inlet", "Reading": 21.0, "ThermalZone": "front"}, "Sensor")
        assert found == ("ThermalZone",)

    def test_a_standard_sensor_is_not(self):
        found = redfish_schema.undeclared_properties(
            {"@odata.id": "/redfish/v1/Chassis/1/Sensors/s0", "Id": "s0",
             "Name": "Inlet", "Reading": 21.0, "ReadingType": "Temperature",
             "Status": {"State": "Enabled"}, "Description": "inlet air"}, "Sensor")
        assert found == ()

    def test_annotations_are_protocol_metadata_and_not_extensions(self):
        found = redfish_schema.undeclared_properties(
            {"Name": "Inlet", "Reading@Redfish.AllowableValues": [1],
             "RelatedItem@odata.count": 2, "@odata.etag": "W/x"}, "Sensor")
        assert found == ()

    def test_oem_is_declared_by_the_schema_and_is_not_descended_into(self):
        """The sanctioned extension point is not drift.

        A vendor putting data under `Oem` did the documented thing. A vendor
        putting it beside `Reading` made an extension where the standard offered a
        place not to -- and only the second one surprises a downstream parser.
        """
        found = redfish_schema.undeclared_properties(
            {"Name": "Inlet", "Oem": {"Vendor": {"Invented": 1}}}, "Sensor")
        assert found == ()

    def test_a_fan_property_is_not_judged_against_the_sensor_schema(self):
        """Six types, six property sets. One merged set would report standard
        properties of one resource as drift on every machine carrying it."""
        fan_only = set(redfish_schema.resource_types()["Fan"]) - set(
            redfish_schema.resource_types()["Voltage"])
        assert fan_only, "Fan and Voltage declare identical properties?"
        name = sorted(fan_only)[0]
        assert redfish_schema.undeclared_properties({name: 1}, "Fan") == ()
        assert redfish_schema.undeclared_properties({name: 1}, "Voltage") == (name,)


class TestAgainstAMachine:
    def test_a_mock_sensor_carrying_an_invented_field_is_named(self):
        bmc = MockBMC(shape="sensors")
        bmc.add("Standard Temp", reading=31.0)
        bmc.add("Drifting Temp", reading=32.0, extra={"FanSpeedPercent": 40})
        with serve(bmc) as url:
            walk = walk_chassis(RedfishClient(url))
        by_name = {s.name: s.undeclared for s in walk}
        assert by_name["Drifting Temp"] == ("FanSpeedPercent",)
        assert by_name["Standard Temp"] == ()

    def test_the_legacy_tree_is_checked_too(self):
        bmc = MockBMC(shape="legacy")
        bmc.add("FAN0", reading=4000.0, units="RPM", extra={"BladePosition": 2})
        with serve(bmc) as url:
            walk = walk_chassis(RedfishClient(url))
        assert [s.undeclared for s in walk] == [("BladePosition",)]
        assert [s.resource for s in walk] == ["Fan"]

    def test_real_2_9_0_firmware_carries_nothing_undeclared(self):
        """The vendored capture of what an actual BMC served.

        Seven objects off one 2021 OpenBMC image -- a small population, and the
        number is stated rather than generalised. What it establishes is that the
        check does not fire on ordinary firmware, which is the property that makes
        a finding worth reading.
        """
        documents = json.loads(
            (FIXTURES / "redfish_witherspoon_2_9_0.json").read_text())["documents"]

        class Replay:
            latencies: list = []

            def get(self, path):
                return dict(documents[path])

        walk = walk_chassis(Replay())
        assert walk.fields_observed
        assert len(walk) >= 6
        assert {s.name: s.undeclared for s in walk if s.undeclared} == {}


class TestTheReportCannotPassVacuously:
    def test_an_old_capture_is_refused_not_passed(self):
        """The load-bearing test. See the module docstring.

        `walk_qemu_bletchley.json` was captured before walks recorded object
        properties. Nothing in it says anything about any property, so the only
        honest report is that the question was not asked.
        """
        walk = walk_from_dict(
            json.loads((FIXTURES / "walk_qemu_bletchley.json").read_text()))
        assert not walk.fields_observed
        rendered = strict_fields_as_text(walk, target="old.json")
        assert "NOT CHECKED" in rendered
        assert "Re-capture" in rendered

        payload = strict_fields_payload(walk)
        assert payload["checked"] is False
        assert "sensors" not in payload, (
            "an unchecked capture must not carry an empty finding list; a "
            "consumer reading only the list cannot tell it from a clean board")

    def test_a_fresh_capture_says_what_it_checked(self):
        bmc = MockBMC(shape="sensors")
        bmc.add("Inlet", reading=21.0)
        with serve(bmc) as url:
            walk = walk_chassis(RedfishClient(url))
        rendered = strict_fields_as_text(walk, target="live")
        assert "NOT CHECKED" not in rendered
        assert "1 sensor object(s) checked" in rendered

        payload = strict_fields_payload(walk)
        assert payload["checked"] is True
        assert payload["objects_checked"] == 1
        assert payload["sensors"] == []
        assert payload["schemas"], "the report does not say which schema it used"

    def test_the_observation_survives_a_capture_and_reload(self):
        bmc = MockBMC(shape="sensors")
        bmc.add("Drifting", reading=1.0, extra={"VendorZone": "a"})
        with serve(bmc) as url:
            walk = walk_chassis(RedfishClient(url))
        reloaded = walk_from_dict(json.loads(json.dumps(walk.to_dict())))
        assert reloaded.fields_observed
        assert [s.undeclared for s in reloaded] == [("VendorZone",)]

    def test_this_projects_own_fixture_markers_are_not_reported_as_drift(self):
        """`_shape` and `_resource` are this repository's, not the machine's.

        Leaving them in the object would have every hand-assembled raw fixture
        report a property the standard does not declare -- a finding invented by
        the tool and then found by it.
        """
        raw = {"sensors": [{"Name": "Inlet", "Reading": 20.0, "_shape": "sensors",
                            "_resource": "Sensor", "@odata.id": "/x"}]}
        walk = walk_from_dict(raw)
        assert [s.undeclared for s in walk] == [()]
