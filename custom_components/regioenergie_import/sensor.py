"""Current price per meter, from its monthly price schedule."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RegioEnergieConfigEntry
from .const import CONF_PRICES, CONF_STATISTIC_ID, CONF_UNIT
from .coordinator import RegioEnergieCoordinator, price_at


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RegioEnergieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a price sensor for every meter with a price schedule."""
    coordinator = entry.runtime_data
    async_add_entities(
        RegioEnergiePriceSensor(coordinator, entry, meter)
        for meter in coordinator.meters
        if meter.get(CONF_PRICES)
    )


class RegioEnergiePriceSensor(CoordinatorEntity[RegioEnergieCoordinator], SensorEntity):
    """The price per unit that applies in the current month.

    For information and automations: the energy dashboard is priced by the
    cost statistic, which applies the price of each month to its own hours.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "price"
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        coordinator: RegioEnergieCoordinator,
        entry: RegioEnergieConfigEntry,
        meter: dict[str, Any],
    ) -> None:
        """Initialise the sensor for one meter."""
        super().__init__(coordinator)
        self._meter = meter
        self._attr_unique_id = f"{entry.entry_id}_{meter[CONF_STATISTIC_ID]}_price"
        self._attr_translation_placeholders = {"meter": meter.get(CONF_NAME, "")}
        self._attr_native_unit_of_measurement = (
            f"{coordinator.hass.config.currency}/{meter[CONF_UNIT]}"
        )

    @property
    def native_value(self) -> float | None:
        """Return the price of the running month."""
        return price_at(self._meter, datetime.now(UTC))

    @property
    def available(self) -> bool:
        """The price is known without the portal, so it is always available."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the whole schedule."""
        return {"schedule": self._meter.get(CONF_PRICES, [])}
