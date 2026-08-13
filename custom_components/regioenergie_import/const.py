"""Constants for the Regio Energie Smart Meter Import integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "regioenergie_import"

# --- Config entry data keys -------------------------------------------------
CONF_BASE_URL: Final = "base_url"

# --- Config entry option keys -----------------------------------------------
CONF_LOGIN_URL: Final = "login_url"
CONF_USERNAME_FIELD: Final = "username_field"
CONF_PASSWORD_FIELD: Final = "password_field"
CONF_DAYS_TO_FETCH: Final = "days_to_fetch"
CONF_SCAN_INTERVAL_HOURS: Final = "scan_interval_hours"
CONF_METERS: Final = "meters"

# --- Per meter keys ---------------------------------------------------------
CONF_METER_NUMBER: Final = "meter_number"
CONF_METER_TYPE: Final = "meter_type"
CONF_OBIS_CODE: Final = "obis_code"
CONF_EXPORT_TYPE: Final = "export_type"
CONF_UNIT: Final = "unit"
CONF_STATISTIC_ID: Final = "statistic_id"

# --- Portal --------------------------------------------------------------- #
DEFAULT_BASE_URL: Final = "https://okp.regioenergie.ch"

# Path of the load profile ("Lastgang") CSV export on the portal.
EXPORT_PATH: Final = "/de/services/lastgang-export.php"

# Pages that are probed for a login form when no explicit login URL is set.
# The first entry is the login page of okp.regioenergie.ch; the rest are
# guesses for portals that put it somewhere else.
LOGIN_PATH_CANDIDATES: Final = (
    "/de/services/login.php",
    "/de/login.php",
    "/de/anmelden.php",
    "/login.php",
    "/de/",
    "/",
)

# The portal reports timestamps in Swiss local time, regardless of where Home
# Assistant itself is running.
PORTAL_TIMEZONE: Final = "Europe/Zurich"

# The export timestamps the *end* of each interval: the rows 01:15, 01:30,
# 01:45 and 02:00 all belong to the hour 01:00. Shifting every timestamp back
# by one quarter of an hour makes them floor onto the correct full hour.
INTERVAL_END_OFFSET: Final = timedelta(minutes=15)

# --- Defaults -------------------------------------------------------------- #
DEFAULT_OBIS_CODE: Final = "1-1:1.29.0"
DEFAULT_EXPORT_TYPE: Final = "WirkBezug"
DEFAULT_DAYS_TO_FETCH: Final = 7
DEFAULT_SCAN_INTERVAL_HOURS: Final = 6

MIN_DAYS_TO_FETCH: Final = 1
MAX_DAYS_TO_FETCH: Final = 365
MIN_SCAN_INTERVAL_HOURS: Final = 1
MAX_SCAN_INTERVAL_HOURS: Final = 24

# --- Meter types ----------------------------------------------------------- #
METER_TYPE_ELECTRICITY: Final = "electricity"
METER_TYPE_GAS: Final = "gas"
METER_TYPE_WATER: Final = "water"
METER_TYPE_HEAT: Final = "heat"

METER_TYPES: Final = (
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
    METER_TYPE_WATER,
    METER_TYPE_HEAT,
)

# Units the Home Assistant energy dashboard accepts per source type. Plain
# strings on purpose - the matching `UnitOf*` constants moved between Home
# Assistant releases, the literals did not.
UNITS_BY_METER_TYPE: Final[dict[str, tuple[str, ...]]] = {
    METER_TYPE_ELECTRICITY: ("kWh", "Wh", "MWh"),
    METER_TYPE_GAS: ("m³", "kWh", "MWh"),
    METER_TYPE_WATER: ("m³", "L"),
    METER_TYPE_HEAT: ("kWh", "MWh", "GJ"),
}

DEFAULT_UNIT_BY_METER_TYPE: Final[dict[str, str]] = {
    METER_TYPE_ELECTRICITY: "kWh",
    METER_TYPE_GAS: "m³",
    METER_TYPE_WATER: "m³",
    METER_TYPE_HEAT: "kWh",
}

ALL_UNITS: Final = tuple(
    dict.fromkeys(unit for units in UNITS_BY_METER_TYPE.values() for unit in units)
)
