"""Diagnostics for the Regio Energie Smart Meter Import."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import RegioEnergieConfigEntry
from .const import CONF_METER_NUMBER, CONF_METERS

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_METER_NUMBER, "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> dict[str, Any]:
    """Return the configuration and the state of the last imports."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "meters": async_redact_data(entry.options.get(CONF_METERS, []), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "last_import": coordinator.data,
        "history": coordinator.backfill_status,
    }
