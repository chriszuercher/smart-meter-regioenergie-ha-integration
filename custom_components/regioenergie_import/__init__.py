"""The Regio Energie Smart Meter Import integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import RegioEnergieClient
from .const import (
    CONF_BASE_URL,
    CONF_LOGIN_URL,
    CONF_PASSWORD_FIELD,
    CONF_USERNAME_FIELD,
    DEFAULT_BASE_URL,
)
from .coordinator import RegioEnergieCoordinator

type RegioEnergieConfigEntry = ConfigEntry[RegioEnergieCoordinator]


def build_client(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> RegioEnergieClient:
    """Create a portal client with its own cookie jar for this entry."""
    session = async_create_clientsession(hass)
    entry.async_on_unload(session.close)
    return RegioEnergieClient(
        session,
        entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        login_url=entry.options.get(CONF_LOGIN_URL),
        username_field=entry.options.get(CONF_USERNAME_FIELD),
        password_field=entry.options.get(CONF_PASSWORD_FIELD),
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> bool:
    """Set up a portal account from a config entry."""
    coordinator = RegioEnergieCoordinator(hass, entry, build_client(hass, entry))
    entry.runtime_data = coordinator

    # This integration writes statistics instead of creating entities, so
    # nothing subscribes to the coordinator on its own - and a coordinator
    # without listeners never schedules its next refresh. The no-op listener
    # keeps the polling timer running.
    entry.async_on_unload(coordinator.async_add_listener(_noop))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await coordinator.async_config_entry_first_refresh()
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> bool:
    """Unload a config entry."""
    return True


async def async_reload_entry(
    hass: HomeAssistant, entry: RegioEnergieConfigEntry
) -> None:
    """Reload the entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _noop() -> None:
    """Do nothing; see async_setup_entry."""
