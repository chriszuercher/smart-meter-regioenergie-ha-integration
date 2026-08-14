"""Fetch load profile data and feed it into the Home Assistant statistics."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MeterReading,
    RegioEnergieAuthError,
    RegioEnergieClient,
    RegioEnergieError,
)
from .const import (
    CONF_COST_STATISTIC_ID,
    CONF_DAYS_TO_FETCH,
    CONF_EXPORT_TYPE,
    CONF_METER_NUMBER,
    CONF_METERS,
    CONF_OBIS_CODE,
    CONF_PRICE,
    CONF_PRICE_WINTER,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    DEFAULT_DAYS_TO_FETCH,
    DEFAULT_EXPORT_TYPE,
    DEFAULT_OBIS_CODE,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    INTERVAL_END_OFFSET,
    PORTAL_TIMEZONE,
    SUMMER_MONTHS,
)

_LOGGER = logging.getLogger(__name__)

# How far back the running total is looked up before giving up and starting a
# new sum at zero. Widening step by step keeps the common case cheap.
_BASELINE_LOOKBACK = (
    timedelta(hours=1),
    timedelta(days=2),
    timedelta(days=30),
    timedelta(days=400),
)

# The statistic metadata grew over time: `has_mean` was replaced by
# `mean_type`, and `unit_class` was added. Sending a key the running core does
# not know breaks the insert, so the supported keys are read off the metadata
# definition itself.
_METADATA_KEYS = set(StatisticMetaData.__annotations__)
_SUPPORTS_MEAN_TYPE = "mean_type" in _METADATA_KEYS
_SUPPORTS_UNIT_CLASS = "unit_class" in _METADATA_KEYS

if _SUPPORTS_MEAN_TYPE:  # pragma: no cover - depends on the core version
    from homeassistant.components.recorder.models import (
        StatisticMeanType,
    )
if _SUPPORTS_UNIT_CLASS:  # pragma: no cover - depends on the core version
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
    )


def build_metadata(statistic_id: str, name: str, unit: str) -> StatisticMetaData:
    """Describe an external statistic for the recorder."""
    metadata: dict[str, Any] = {
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if _SUPPORTS_MEAN_TYPE:
        metadata["mean_type"] = StatisticMeanType.NONE
    else:
        metadata["has_mean"] = False
    if _SUPPORTS_UNIT_CLASS:
        # Derived exactly like the recorder would, so the unit and its class
        # can never contradict each other.
        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        metadata["unit_class"] = converter.UNIT_CLASS if converter else None
    return cast(StatisticMetaData, metadata)


def cost_statistic_id_for(statistic_id: str) -> str:
    """Return the id of the cost statistic belonging to a consumption one."""
    return statistic_id.removesuffix("_import") + "_cost"


def price_at(meter: dict[str, Any], hour: datetime) -> float | None:
    """Return the price per unit that applies at a given hour.

    None means the meter has no price configured and gets no cost statistic.
    """
    base = meter.get(CONF_PRICE)
    if base is None:
        return None
    winter = meter.get(CONF_PRICE_WINTER)
    if winter is None or hour.month in SUMMER_MONTHS:
        return float(base)
    return float(winter)


def drop_unpublished_tail(readings: list[MeterReading]) -> list[MeterReading]:
    """Cut the trailing intervals the utility has not published yet.

    The export always spans the full requested range, so its tail is padded
    with empty values. Those mean "not measured yet", not "consumed nothing",
    and importing them as zero would show a flat line for the most recent day
    until the next poll corrects it. Empty values *between* readings are kept
    at zero, the way the original script treated them.
    """
    ordered = sorted(readings, key=lambda reading: reading.timestamp)
    end = len(ordered)
    while end and ordered[end - 1].is_empty:
        end -= 1
    return ordered[:end]


def build_hourly_totals(readings: list[MeterReading]) -> dict[datetime, float]:
    """Group interval readings into full hours.

    The export timestamps the end of each interval, so 01:15, 01:30, 01:45 and
    02:00 all belong to the hour 01:00. Shifting every timestamp back by 15
    minutes before flooring puts them onto the right hour - this quirk is the
    whole reason the original script existed and is kept verbatim.
    """
    totals: dict[datetime, float] = {}
    for reading in readings:
        hour = (reading.timestamp - INTERVAL_END_OFFSET).replace(
            minute=0, second=0, microsecond=0
        )
        totals[hour] = totals.get(hour, 0.0) + reading.value
    return totals


class RegioEnergieCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll the portal and import the result as external statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: RegioEnergieClient,
    ) -> None:
        """Initialise the coordinator from the config entry options."""
        options = entry.options
        self.client = client
        self._meters: list[dict[str, Any]] = list(options.get(CONF_METERS, []))
        self._days_to_fetch: int = int(
            options.get(CONF_DAYS_TO_FETCH, DEFAULT_DAYS_TO_FETCH)
        )
        self._timezone = ZoneInfo(PORTAL_TIMEZONE)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                hours=int(
                    options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
                )
            ),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch every configured meter and import its statistics."""
        if not self._meters:
            _LOGGER.warning(
                "No meters configured - add one under the integration options"
            )
            return {}

        now = datetime.now(self._timezone)
        start = (now - timedelta(days=self._days_to_fetch)).date()
        end = now.date()

        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        for meter in self._meters:
            statistic_id = meter[CONF_STATISTIC_ID]
            name = meter.get("name", statistic_id)
            try:
                results[statistic_id] = await self._async_import_meter(
                    meter, start, end, now
                )
            except RegioEnergieAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except RegioEnergieError as err:
                _LOGGER.error("Import failed for %s: %s", name, err)
                errors.append(f"{name}: {err}")

        # Only fail the whole update when nothing at all could be fetched;
        # a single broken meter should not hide the data of the others.
        if errors and not results:
            raise UpdateFailed("; ".join(errors))

        return results

    async def _async_import_meter(
        self, meter: dict[str, Any], start: date, end: date, now: datetime
    ) -> dict[str, Any]:
        """Fetch one meter and hand its hourly sums to the recorder."""
        statistic_id: str = meter[CONF_STATISTIC_ID]
        name: str = meter.get("name", statistic_id)

        readings = await self.client.async_fetch_readings(
            meter[CONF_METER_NUMBER],
            meter.get(CONF_OBIS_CODE, DEFAULT_OBIS_CODE),
            meter.get(CONF_EXPORT_TYPE, DEFAULT_EXPORT_TYPE),
            start,
            end,
        )

        hourly_totals = build_hourly_totals(drop_unpublished_tail(readings))
        # The hour that is still running would be imported short and only be
        # corrected by the next poll, so it waits until it has fully elapsed.
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        hours = [hour for hour in sorted(hourly_totals) if hour < current_hour]
        if not hours:
            _LOGGER.debug("No complete hours to import for %s", name)
            return {"statistic_id": statistic_id, "imported": 0, "last_sum": None}

        running_sum = await self._async_get_baseline_sum(statistic_id, hours[0])

        statistics: list[StatisticData] = []
        for hour in hours:
            running_sum += hourly_totals[hour]
            statistics.append(StatisticData(start=hour, sum=round(running_sum, 3)))

        async_add_external_statistics(
            self.hass,
            build_metadata(statistic_id, name, meter[CONF_UNIT]),
            statistics,
        )
        _LOGGER.debug(
            "Imported %s hourly statistics for %s (last sum %s)",
            len(statistics),
            name,
            statistics[-1]["sum"],
        )

        result = {
            "statistic_id": statistic_id,
            "imported": len(statistics),
            "last_sum": statistics[-1]["sum"],
            "last_hour": hours[-1].isoformat(),
        }
        result["last_cost_sum"] = await self._async_import_cost(
            meter, name, hours, hourly_totals
        )
        return result

    async def _async_import_cost(
        self,
        meter: dict[str, Any],
        name: str,
        hours: list[datetime],
        hourly_totals: dict[datetime, float],
    ) -> float | None:
        """Import what the consumption of those hours costs, if priced.

        The energy dashboard cannot price an external statistic on its own: its
        cost sensor reads the state of an entity, and a statistic has none. So
        the cost is imported as a statistic of its own, which the dashboard
        accepts as "a statistic tracking total costs". Doing it here also means
        every hour is priced with the tariff that applied back then.
        """
        if price_at(meter, hours[0]) is None:
            return None

        cost_statistic_id = meter.get(CONF_COST_STATISTIC_ID) or cost_statistic_id_for(
            meter[CONF_STATISTIC_ID]
        )
        running_sum = await self._async_get_baseline_sum(cost_statistic_id, hours[0])

        statistics: list[StatisticData] = []
        for hour in hours:
            running_sum += hourly_totals[hour] * (price_at(meter, hour) or 0.0)
            statistics.append(StatisticData(start=hour, sum=round(running_sum, 4)))

        async_add_external_statistics(
            self.hass,
            build_metadata(
                cost_statistic_id,
                f"{name} cost",
                self.hass.config.currency,
            ),
            statistics,
        )
        _LOGGER.debug(
            "Imported %s hourly cost statistics for %s (last sum %s)",
            len(statistics),
            name,
            statistics[-1]["sum"],
        )
        return statistics[-1]["sum"]

    async def _async_get_baseline_sum(
        self, statistic_id: str, first_hour: datetime
    ) -> float:
        """Return the running total right before the window being imported.

        The recorder is the single source of truth for the total, so the
        original script's local JSON state file is no longer needed. Because
        every run recomputes the sums of the whole fetched window on top of
        this baseline, values the utility corrects afterwards land correctly.
        """
        recorder = get_instance(self.hass)
        for lookback in _BASELINE_LOOKBACK:
            rows = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                first_hour - lookback,
                first_hour,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            existing = rows.get(statistic_id)
            if existing:
                last_sum = existing[-1].get("sum")
                if last_sum is not None:
                    return float(last_sum)

        _LOGGER.debug("No previous statistics for %s, starting at 0", statistic_id)
        return 0.0
