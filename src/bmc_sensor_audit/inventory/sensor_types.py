"""Which `entity-manager` `Type` values actually produce a sensor reading.

**The defect this exists to fix.** `Type` was read and never used. Every `Exposes`
entry counted as a sensor that ought to be reporting, so a PID fan-control loop, a
stepwise fan curve, an EEPROM and a GPIO presence detector were all expected to appear
in a Redfish `Sensors` collection. They cannot. Measured against 349 upstream
configurations, **2,121 of 8,684 declarations cannot report a reading** — 2,069
classified as non-sensors and 52 unrecognised, about 24 %. Because none of them can
ever appear, every one became a `declared_absent` regression. The gate went red on a
healthy board, on three boards in four, permanently.

An earlier count of this defect put it at 1,467 by naming seven types. That was the
visible tail; measuring every type found eighty-two.

**Three values, not two.** A closed sensor/not-a-sensor split would be the same mistake
one level up: a `Type` this build has never seen would be forced into whichever bucket
the default happens to be, confidently and silently. So the answer is *sensor*, *not a
sensor*, or ***unrecognised*** — and the third is the one that matters, because it is
the one that grows. It is reported, counted, and deliberately produces **no absence
finding**: claiming a regression for a type we cannot classify is exactly the false
positive this module was written to remove.

**How the sets were derived, so they can be re-derived.** Basis
`openbmc/entity-manager@0ada0483`.

- `KNOWN_SENSOR` is evidence, not judgement: every `Type` observed declaring at least
  one threshold somewhere in the corpus. A threshold only means something against a
  reading, so a type that carries one produces readings. **94 types, 6,563
  declarations.** Every name in it is observed in the corpus — none is dead weight.
- `NOT_A_SENSOR` is judgement, constrained by evidence: types that **never** declare a
  threshold anywhere in the corpus *and* whose names denote a non-reading object —
  control loops, presence detectors, inventory, firmware, muxes, connectors, actuators.
  **82 types, 2,069 declarations**, reached by 29 explicit names plus eleven suffix
  families so a new firmware blob or mux needs no edit here.
- Everything else is unrecognised. **17 types, 52 declarations** — about 0.6 %, and
  mostly real sensors that simply declare no thresholds in this corpus. They are
  reported rather than guessed at.

**The two sets must not intersect, and a test asserts it.** That check is not
decoration: it caught `XeonCPU` and `ModifiedMedian`, both of which this author had put
in `NOT_A_SENSOR` while the corpus showed them declaring thresholds. A CPU package
temperature and a virtual-sensor aggregation are both sensors. Two wrong judgements,
found by evidence rather than by review.
"""

from __future__ import annotations

SENSOR = "sensor"
NOT_A_SENSOR = "not_a_sensor"
UNRECOGNISED = "unrecognised"

KINDS = (SENSOR, NOT_A_SENSOR, UNRECOGNISED)

# Observed declaring at least one threshold. Evidence, not opinion.
KNOWN_SENSOR = frozenset({
    "ADC", "ADC128D818", "ADM1021", "ADM1266", "ADM1272", "ADM1278",
    "ADM1281", "ADS1015", "ADS7830", "AspeedFan", "DPS310", "E50SN12051",
    "EMC1403", "EMC1413", "ExitAirTempSensor", "G751", "HDC1080", "I2CFan",
    "INA230", "INA233", "INA238", "IR35221", "IR38060", "IR38164", "IR38263",
    "ISL28022", "ISL69260", "IpmbSensor", "JC42", "LM5066I", "LM75A",
    "LTC4282", "LTC4287", "MAX11615", "MAX11617", "MAX31725", "MAX34451",
    "MAX5970", "MAX6639", "MCP9600", "MP2869", "MP2929", "MP2971", "MP2973",
    "MP2993", "MP29612", "MP5023", "MP5926", "MP5990", "MP5998", "MP9941",
    "MP9945", "MPQ8785", "Maximum", "Minimum", "ModifiedMedian", "NCT6779",
    "NCT7802", "NVME1000", "NuvotonFan", "PLI1209BC", "PT5161L", "PXE1610",
    "Q54SN120A1", "Q54SW120A7", "RAA228000", "RAA228004", "RAA228006",
    "RAA228228", "RTQ6056", "SBRMI", "SBTSI", "SI7020", "SQ52206", "SY24655",
    "TDA38640", "TMP100", "TMP112", "TMP1075", "TMP175", "TMP411", "TMP421",
    "TMP432", "TMP75", "TPS25990", "W83773G", "XDP710", "XDPE11280",
    "XDPE132G5C", "XDPE152C4", "XeonCPU", "cffps", "pmbus", "smpro_hwmon",
})

# Never declares a threshold anywhere in the corpus, and names a non-reading object.
_NOT_A_SENSOR_EXPLICIT = frozenset({
    # Fan control and aggregation policy, not measurements.
    "Pid", "Pid.Zone", "Stepwise", "FanRedundancy", "PURedundancy",
    # Presence and discrete GPIO state.
    "GPIODeviceDetect", "GPIOLeakDetector", "PSUPresence", "MultiNodePresence",
    "Gpio",
    # Inventory and storage devices.
    "EEPROM", "EEPROM_24C02", "EEPROM_24C64", "EEPROM_24C128", "EEPROM_24C256",
    "HostSPIFlash", "IntelE810SPIFlash", "EmmcDevice", "IntelHsbpFruDevice",
    # Transports and bus targets.
    "MCTPI2CTarget", "MCTPI3CTarget", "NvidiaMctpVdm", "SatelliteController",
    "IpmbDevice", "GenericSMBusMux",
    # Board and system objects.
    "BMC", "IBMCompatibleSystem", "MultiNodeID", "PowerModeProperties",
})

# Whole families, by suffix. A vendor adding another firmware blob or another mux
# should not need this file edited.
_NOT_A_SENSOR_SUFFIXES = (
    "Firmware", "Mux", "Port", "Valve", "Connector",
    "PowerSupplyUnit", "BatteryBackupUnit", "PowerShelf",
    "PowerMonitorModule", "CapacitorBankUnit", "ReservoirPumpUnit",
)


def classify(sensor_type: str | None) -> str:
    """Return `SENSOR`, `NOT_A_SENSOR` or `UNRECOGNISED` for an entity-manager Type.

    `KNOWN_SENSOR` is checked first, so evidence beats a suffix guess: a real sensor
    whose name happens to end in a family suffix is still a sensor.
    """
    if not sensor_type:
        return UNRECOGNISED
    if sensor_type in KNOWN_SENSOR:
        return SENSOR
    if sensor_type in _NOT_A_SENSOR_EXPLICIT:
        return NOT_A_SENSOR
    if any(sensor_type.endswith(suffix) for suffix in _NOT_A_SENSOR_SUFFIXES):
        return NOT_A_SENSOR
    return UNRECOGNISED


def is_expected_live(sensor_type: str | None) -> bool:
    """Whether absence of this type should count as a regression.

    Only a known sensor. An unrecognised type is *reported*, never asserted about —
    the whole reason the third bucket exists.
    """
    return classify(sensor_type) == SENSOR
