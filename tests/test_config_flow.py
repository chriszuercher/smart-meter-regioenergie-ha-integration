"""Tests for the statistic id generation and the meter form validation."""

import pytest

from custom_components.regioenergie_import.config_flow import (
    _validate_meter_input,
    build_statistic_id,
)
from custom_components.regioenergie_import.const import (
    CONF_METER_NUMBER,
    CONF_METER_TYPE,
    CONF_OBIS_CODE,
    CONF_UNIT,
    DEFAULT_OBIS_CODE,
)
from homeassistant.components.recorder.statistics import valid_statistic_id

VALID_METER = {
    "name": "Water",
    CONF_METER_TYPE: "water",
    CONF_METER_NUMBER: "CH8000051234500000000000000014186",
    CONF_OBIS_CODE: DEFAULT_OBIS_CODE,
    CONF_UNIT: "m³",
}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Water", "regioenergie_import:water_import"),
        ("Wasser (Garten)", "regioenergie_import:wasser_garten_import"),
        ("Fernwärme", "regioenergie_import:fernwarme_import"),
        ("   ", "regioenergie_import:unknown_import"),
    ],
)
def test_statistic_ids_are_valid(name: str, expected: str) -> None:
    """Names turn into ids the recorder accepts."""
    statistic_id = build_statistic_id(name, set())

    assert statistic_id == expected
    assert valid_statistic_id(statistic_id)


def test_statistic_ids_do_not_collide() -> None:
    """A second meter with the same name gets its own id."""
    taken = {"regioenergie_import:water_import"}

    second = build_statistic_id("Water", taken)
    third = build_statistic_id("Water", taken | {second})

    assert second == "regioenergie_import:water_2_import"
    assert third == "regioenergie_import:water_3_import"
    assert valid_statistic_id(second)


def test_meter_input_accepts_a_complete_form() -> None:
    """A filled in form reports no errors."""
    assert _validate_meter_input(VALID_METER) == {}


def test_meter_input_rejects_a_unit_that_does_not_fit() -> None:
    """A water meter cannot be counted in kWh."""
    errors = _validate_meter_input({**VALID_METER, CONF_UNIT: "kWh"})

    assert errors == {CONF_UNIT: "invalid_unit"}


def test_meter_input_reports_missing_fields() -> None:
    """Empty required fields are reported per field."""
    errors = _validate_meter_input(
        {**VALID_METER, "name": "", CONF_METER_NUMBER: "", CONF_OBIS_CODE: ""}
    )

    assert errors == {
        "name": "required",
        CONF_METER_NUMBER: "required",
        CONF_OBIS_CODE: "required",
    }


@pytest.mark.parametrize(
    ("meter_type", "unit"),
    [("electricity", "kWh"), ("gas", "m³"), ("water", "m³"), ("heat", "kWh")],
)
def test_every_meter_type_has_a_working_default(meter_type: str, unit: str) -> None:
    """All four meter types can be configured with their default unit."""
    meter = {**VALID_METER, CONF_METER_TYPE: meter_type, CONF_UNIT: unit}

    assert _validate_meter_input(meter) == {}
