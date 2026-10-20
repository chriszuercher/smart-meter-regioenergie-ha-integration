"""Fetch load profile data and feed it into the Home Assistant statistics."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify

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
    CONF_HISTORY_DAYS,
    CONF_METER_NUMBER,
    CONF_METERS,
    CONF_OBIS_CODE,
    CONF_PRICE,
    CONF_PRICES,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    CONF_VALID_FROM,
    CORRECTION_HOUR,
    DEFAULT_DAYS_TO_FETCH,
    DEFAULT_EXPORT_TYPE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_OBIS_CODE,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    FAILURES_BEFORE_ISSUE,
    HISTORY_CHUNK_DAYS,
    INTERVAL_END_OFFSET,
    PORTAL_TIMEZONE,
    RECENT_DAYS_TO_FETCH,
)

_LOGGER = logging.getLogger(__name__)

_PORTAL_TZ = ZoneInfo(PORTAL_TIMEZONE)

# How far back the running total is looked up before giving up and starting a
# new sum at zero. Widening step by step keeps the common case cheap.
_BASELINE_LOOKBACK = (
    timedelta(hours=1),
    timedelta(days=2),
    timedelta(days=30),
    timedelta(days=400),
)

# Re-pricing "the whole history" starts here; nothing older can exist.
_HISTORY_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Month key that sorts before every real one: "re-price from the beginning".
FROM_THE_BEGINNING = "0000-01"

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


def cost_statistic_id_of(meter: dict[str, Any]) -> str:
    """Return the cost statistic id stored with a meter, or its derived one."""
    return meter.get(CONF_COST_STATISTIC_ID) or cost_statistic_id_for(
        meter[CONF_STATISTIC_ID]
    )


def month_key(moment: datetime | date) -> str:
    """Return the "YYYY-MM" key a price schedule is keyed by.

    Times are taken in Swiss local time: the first hour of a month in
    Switzerland is still the last hour of the previous month in UTC.
    """
    if isinstance(moment, datetime):
        moment = moment.astimezone(_PORTAL_TZ)
    return f"{moment.year:04d}-{moment.month:02d}"


def sorted_schedule(schedule: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return the price entries ordered by the month they start in."""
    return sorted(schedule or [], key=lambda entry: entry[CONF_VALID_FROM])


def price_for_month(schedule: list[dict[str, Any]] | None, month: str) -> float | None:
    """Return the price of a month: the latest entry that started by then.

    Months between two entries carry the earlier price over. Months before the
    first entry use the first price, so a price entered today also prices the
    history imported before it.
    """
    entries = sorted_schedule(schedule)
    if not entries:
        return None
    price = entries[0][CONF_PRICE]
    for entry in entries:
        if entry[CONF_VALID_FROM] > month:
            break
        price = entry[CONF_PRICE]
    return float(price)


def price_at(meter: dict[str, Any], hour: datetime) -> float | None:
    """Return the price per unit that applies at a given hour.

    None means the meter has no price configured and gets no cost statistic.
    """
    return price_for_month(meter.get(CONF_PRICES), month_key(hour))


def first_repriced_month(
    old: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None
) -> str | None:
    """Return the first month whose price differs between two schedules.

    None means nothing has to be re-priced; FROM_THE_BEGINNING means the
    months before the first entry changed as well.
    """
    if not new:
        return None
    if not old:
        return FROM_THE_BEGINNING
    # A price can only change in a month in which one of the schedules has
    # an entry, so those months and "before all of them" are all to compare.
    months = sorted(
        {entry[CONF_VALID_FROM] for entry in [*old, *new]} | {FROM_THE_BEGINNING}
    )
    for month in months:
        if price_for_month(old, month) != price_for_month(new, month):
            return month
    return None


def store_key(entry_id: str) -> str:
    """Return the storage key of a config entry's import state."""
    return f"{DOMAIN}.{entry_id}"


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
    """Group interval readings into full hours, keyed by the UTC hour start.

    The export timestamps the end of each interval, so 01:15, 01:30, 01:45 and
    02:00 all belong to the hour 01:00. Shifting every timestamp back by 15
    minutes before flooring puts them onto the right hour - this quirk is the
    whole reason the original script existed.

    The shift and the flooring happen in UTC. In local time "02:00 minus 15
    minutes" does not exist on the day the clocks go forward, and the hour
    that repeats in autumn would be merged into one. Swiss offsets are whole
    hours, so a full UTC hour is a full local hour as well.
    """
    totals: dict[datetime, float] = {}
    for reading in readings:
        hour = (reading.timestamp.astimezone(UTC) - INTERVAL_END_OFFSET).replace(
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
        self.history_days: int = int(
            options.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        )
        self._timezone = _PORTAL_TZ
        self._last_correction: date | None = None
        # Statistic ids known to have data, so the backfill check is skipped.
        self._has_history: set[str] = set()
        self._backfilling: set[str] = set()
        self._failures: dict[str, int] = {}
        self.backfill_status: dict[str, dict[str, Any]] = {}
        # Survives restarts: the price schedule each cost statistic was last
        # computed with, and where an unfinished history import resumes.
        self._store: Store[dict[str, Any]] = Store(hass, 1, store_key(entry.entry_id))
        self._state: dict[str, Any] | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=int(
                    options.get(
                        CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
                    )
                )
            ),
        )

    @property
    def meters(self) -> list[dict[str, Any]]:
        """Return the configured meters."""
        return self._meters

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch every configured meter and import its statistics."""
        if not self._meters:
            _LOGGER.warning(
                "No meters configured - add one under the integration options"
            )
            return {}

        now = datetime.now(UTC)
        local_now = now.astimezone(self._timezone)
        # Once a day the whole window is read again for corrections; the
        # other polls only need the last couple of days.
        correction = (
            self._last_correction != local_now.date()
            and local_now.hour >= CORRECTION_HOUR
        )
        days = (
            self._days_to_fetch
            if correction
            else min(RECENT_DAYS_TO_FETCH, self._days_to_fetch)
        )
        start = (local_now - timedelta(days=days)).date()
        end = local_now.date()

        previous = self.data or {}
        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        for meter in self._meters:
            statistic_id = meter[CONF_STATISTIC_ID]
            name = meter.get("name", statistic_id)
            if statistic_id in self._backfilling:
                if statistic_id in previous:
                    results[statistic_id] = previous[statistic_id]
                continue
            try:
                backfill_from = await self._async_backfill_start(meter)
                if backfill_from is not None:
                    self.async_start_backfill(meter, backfill_from)
                    continue
                repriced = await self._async_reprice_if_needed(meter)
                results[statistic_id] = await self._async_import_meter(
                    meter, start, end, now, import_cost=not repriced
                )
            except RegioEnergieAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except RegioEnergieError as err:
                _LOGGER.error("Import failed for %s: %s", name, err)
                errors.append(f"{name}: {err}")
                self._record_failure(meter, err)
            else:
                self._record_success(meter)

        # Only fail the whole update when nothing at all could be fetched;
        # a single broken meter should not hide the data of the others.
        if errors and not results:
            raise UpdateFailed("; ".join(errors))
        if correction and not errors:
            self._last_correction = local_now.date()

        return results

    async def _async_import_meter(
        self,
        meter: dict[str, Any],
        start: date,
        end: date,
        now: datetime,
        *,
        import_cost: bool = True,
    ) -> dict[str, Any]:
        """Fetch one meter and hand its hourly sums to the recorder."""
        statistic_id: str = meter[CONF_STATISTIC_ID]
        hours, hourly_totals = await self._async_fetch_hours(meter, start, end, now)
        if not hours:
            _LOGGER.debug("No complete hours to import for %s", statistic_id)
            return {"statistic_id": statistic_id, "imported": 0, "last_sum": None}

        consumption = await self._async_get_baseline_sum(statistic_id, hours[0])
        cost = None
        if import_cost and price_at(meter, hours[0]) is not None:
            cost = await self._async_get_baseline_sum(
                cost_statistic_id_of(meter), hours[0]
            )
        last_sum, last_cost_sum = self._write_statistics(
            meter, hours, hourly_totals, consumption, cost
        )
        self._has_history.add(statistic_id)
        return {
            "statistic_id": statistic_id,
            "imported": len(hours),
            "last_sum": last_sum,
            "last_hour": hours[-1].astimezone(self._timezone).isoformat(),
            "last_cost_sum": last_cost_sum,
        }

    async def _async_fetch_hours(
        self, meter: dict[str, Any], start: date, end: date, now: datetime
    ) -> tuple[list[datetime], dict[datetime, float]]:
        """Fetch a meter and return its complete hours with their totals."""
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
        current_hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        hours = [hour for hour in sorted(hourly_totals) if hour < current_hour]
        return hours, hourly_totals

    def _write_statistics(
        self,
        meter: dict[str, Any],
        hours: list[datetime],
        hourly_totals: dict[datetime, float],
        consumption: float,
        cost: float | None,
    ) -> tuple[float, float | None]:
        """Queue the running sums of these hours on top of the given baselines.

        Returns the last consumption and cost sums, unrounded, so a caller
        importing in several steps can carry them over. With `cost` None no
        cost statistic is written.
        """
        statistic_id: str = meter[CONF_STATISTIC_ID]
        name: str = meter.get("name", statistic_id)

        statistics: list[StatisticData] = []
        cost_statistics: list[StatisticData] = []
        for hour in hours:
            consumption += hourly_totals[hour]
            statistics.append(StatisticData(start=hour, sum=round(consumption, 3)))
            if cost is not None:
                cost += hourly_totals[hour] * (price_at(meter, hour) or 0.0)
                cost_statistics.append(StatisticData(start=hour, sum=round(cost, 4)))

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

        # The energy dashboard cannot price an external statistic on its own:
        # its cost sensor reads the state of an entity, and a statistic has
        # none. So the cost is imported as a statistic of its own, which the
        # dashboard accepts as "a statistic tracking total costs". Every hour
        # is priced with the price of its own month.
        if cost_statistics:
            async_add_external_statistics(
                self.hass,
                build_metadata(
                    cost_statistic_id_of(meter),
                    f"{name} cost",
                    self.hass.config.currency,
                ),
                cost_statistics,
            )
            _LOGGER.debug(
                "Imported %s hourly cost statistics for %s (last sum %s)",
                len(cost_statistics),
                name,
                cost_statistics[-1]["sum"],
            )
        return consumption, cost if cost_statistics else None

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

    # --- history backfill ----------------------------------------------------

    async def _async_backfill_start(self, meter: dict[str, Any]) -> date | None:
        """Return where a meter's history import has to start, if it needs one.

        A meter without any statistic yet - a new entry or a meter added
        later - gets its history fetched first. So does one whose backfill
        failed half way, from where it stopped.
        """
        statistic_id = meter[CONF_STATISTIC_ID]
        pending = (await self._async_state())["pending_history"]
        if statistic_id in pending:
            return date.fromisoformat(pending[statistic_id])
        if statistic_id in self._has_history or self.history_days <= 0:
            return None

        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"sum"}
        )
        if rows.get(statistic_id):
            self._has_history.add(statistic_id)
            return None
        today = datetime.now(self._timezone).date()
        return today - timedelta(days=self.history_days)

    def async_start_backfill(self, meter: dict[str, Any], start: date) -> bool:
        """Import a meter's history from `start` up to now in the background.

        Returns False if a backfill of this meter is already running.
        """
        statistic_id = meter[CONF_STATISTIC_ID]
        if statistic_id in self._backfilling:
            return False
        self._backfilling.add(statistic_id)
        assert self.config_entry is not None
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_backfill(meter, start),
            name=f"{DOMAIN} history of {statistic_id}",
        )
        return True

    async def _async_backfill(self, meter: dict[str, Any], start: date) -> None:
        """Fetch the history oldest first, in chunks, and import it.

        The running sums are carried from chunk to chunk in memory: the
        recorder writes in the background, so reading the baseline back after
        each chunk could see the previous chunk not yet stored.
        """
        statistic_id = meter[CONF_STATISTIC_ID]
        name = meter.get("name", statistic_id)
        status = self.backfill_status[statistic_id] = {
            "state": "running",
            "start": start.isoformat(),
            "imported_until": None,
            "hours": 0,
            "error": None,
        }
        _LOGGER.info("Importing the history of %s since %s", name, start)
        await self._async_set_pending_history(statistic_id, start)

        consumption: float | None = None
        cost: float | None = None
        priced = bool(meter.get(CONF_PRICES))
        last_hour: datetime | None = None
        chunk_start = start
        try:
            today = datetime.now(self._timezone).date()
            while chunk_start <= today:
                chunk_end = min(chunk_start + timedelta(days=HISTORY_CHUNK_DAYS), today)
                hours, totals = await self._async_fetch_hours(
                    meter, chunk_start, chunk_end, datetime.now(UTC)
                )
                # Chunks overlap by a day, whether the portal's end date is
                # inclusive or not; hours already imported are skipped.
                hours = [
                    hour for hour in hours if last_hour is None or hour > last_hour
                ]
                if hours:
                    if consumption is None:
                        consumption = await self._async_get_baseline_sum(
                            statistic_id, hours[0]
                        )
                        if priced:
                            cost = await self._async_get_baseline_sum(
                                cost_statistic_id_of(meter), hours[0]
                            )
                    consumption, cost = self._write_statistics(
                        meter, hours, totals, consumption, cost
                    )
                    last_hour = hours[-1]
                    status["hours"] += len(hours)
                    status["imported_until"] = last_hour.astimezone(
                        self._timezone
                    ).isoformat()
                    _LOGGER.debug(
                        "History of %s imported up to %s",
                        name,
                        status["imported_until"],
                    )
                if chunk_end >= today:
                    break
                chunk_start = chunk_end
                # A restart in between picks up from here instead of leaving
                # the rest of the history out.
                await self._async_set_pending_history(statistic_id, chunk_start)
        except RegioEnergieAuthError as err:
            # Resumed from the stored chunk once the entry reloads after the
            # new credentials are in.
            status.update(state="failed", error=str(err))
            assert self.config_entry is not None
            self.config_entry.async_start_reauth(self.hass)
        except RegioEnergieError as err:
            # The next poll picks up from the chunk that failed.
            _LOGGER.error(
                "History import of %s stopped at %s: %s", name, chunk_start, err
            )
            status.update(state="failed", error=str(err))
        else:
            status["state"] = "done"
            await self._async_set_pending_history(statistic_id, None)
            if last_hour is not None:
                self._has_history.add(statistic_id)
            if priced:
                await self._async_remember_prices(meter)
            _LOGGER.info("History of %s imported: %s hours", name, status["hours"])
        finally:
            self._backfilling.discard(statistic_id)

    # --- re-pricing ------------------------------------------------------------

    async def _async_state(self) -> dict[str, Any]:
        """Return the stored import state, loading it on first use."""
        if self._state is None:
            stored = await self._store.async_load() or {}
            self._state = {
                "prices": stored.get("prices", {}),
                "pending_history": stored.get("pending_history", {}),
            }
        return self._state

    async def _async_set_pending_history(
        self, statistic_id: str, resume_from: date | None
    ) -> None:
        """Store where a history import resumes, or that it is complete."""
        pending = (await self._async_state())["pending_history"]
        if resume_from is None:
            pending.pop(statistic_id, None)
        else:
            pending[statistic_id] = resume_from.isoformat()
        await self._store.async_save(self._state)

    async def _async_remember_prices(self, meter: dict[str, Any]) -> None:
        """Store the schedule a meter's cost statistic now reflects."""
        applied = (await self._async_state())["prices"]
        applied[cost_statistic_id_of(meter)] = sorted_schedule(meter.get(CONF_PRICES))
        await self._store.async_save(self._state)

    async def _async_reprice_if_needed(self, meter: dict[str, Any]) -> bool:
        """Re-price the cost history after the price schedule changed.

        Returns True if the cost statistic was rebuilt; the regular cost import
        then waits for the next poll, as its baseline is being rewritten.
        """
        cost_statistic_id = cost_statistic_id_of(meter)
        applied = (await self._async_state())["prices"]
        schedule = sorted_schedule(meter.get(CONF_PRICES))
        if cost_statistic_id not in applied:
            # First time this meter is seen with this version: its cost, if
            # any, was computed with the schedule it has now.
            applied[cost_statistic_id] = schedule
            await self._store.async_save(self._state)
            return False

        month = first_repriced_month(applied[cost_statistic_id], schedule)
        if month is None:
            if applied[cost_statistic_id] != schedule:
                await self._async_remember_prices(meter)
            return False

        if month == FROM_THE_BEGINNING:
            since = _HISTORY_EPOCH
        else:
            year, month_number = (int(part) for part in month.split("-"))
            since = datetime(year, month_number, 1, tzinfo=self._timezone).astimezone(
                UTC
            )
        await self.async_rebuild_cost(meter, since)
        await self._async_remember_prices(meter)
        return True

    async def async_rebuild_cost(self, meter: dict[str, Any], since: datetime) -> None:
        """Recompute the cost statistic from `since` on with the current prices.

        The consumption per hour is read back from the meter's own statistic,
        so the portal is not asked for the history again.
        """
        statistic_id = meter[CONF_STATISTIC_ID]
        cost_statistic_id = cost_statistic_id_of(meter)
        rows = (
            await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                since,
                None,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
        ).get(statistic_id, [])
        if not rows:
            return

        first_hour = datetime.fromtimestamp(rows[0]["start"], UTC)
        previous = await self._async_get_baseline_sum(statistic_id, first_hour)
        cost = await self._async_get_baseline_sum(cost_statistic_id, first_hour)
        statistics: list[StatisticData] = []
        for row in rows:
            total = row.get("sum")
            if total is None:
                continue
            hour = datetime.fromtimestamp(row["start"], UTC)
            cost += (total - previous) * (price_at(meter, hour) or 0.0)
            previous = total
            statistics.append(StatisticData(start=hour, sum=round(cost, 4)))

        name = meter.get("name", statistic_id)
        async_add_external_statistics(
            self.hass,
            build_metadata(
                cost_statistic_id, f"{name} cost", self.hass.config.currency
            ),
            statistics,
        )
        _LOGGER.info(
            "Re-priced %s hours of %s from %s on",
            len(statistics),
            name,
            first_hour.astimezone(self._timezone).date(),
        )

    # --- repair issues ---------------------------------------------------------

    def _issue_id(self, meter: dict[str, Any]) -> str:
        return f"meter_failed_{slugify(meter[CONF_STATISTIC_ID])}"

    def _record_failure(self, meter: dict[str, Any], err: Exception) -> None:
        """Raise a repair issue once a meter keeps failing."""
        statistic_id = meter[CONF_STATISTIC_ID]
        count = self._failures[statistic_id] = self._failures.get(statistic_id, 0) + 1
        if count == FAILURES_BEFORE_ISSUE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id(meter),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="meter_failed",
                translation_placeholders={
                    "name": meter.get("name", statistic_id),
                    "error": str(err),
                },
            )

    def _record_success(self, meter: dict[str, Any]) -> None:
        """Clear the failure count and any repair issue of a meter."""
        if self._failures.pop(meter[CONF_STATISTIC_ID], 0) >= FAILURES_BEFORE_ISSUE:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(meter))
