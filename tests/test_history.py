"""Tests for the history import, the re-pricing and the entry migration."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.regioenergie_import import coordinator as coordinator_module
from custom_components.regioenergie_import import migrate_options_v1
from custom_components.regioenergie_import.api import (
    MeterReading,
    RegioEnergieConnectionError,
)
from custom_components.regioenergie_import.const import (
    CONF_DAYS_TO_FETCH,
    CONF_HISTORY_DAYS,
    CONF_METERS,
    CONF_PRICE,
    CONF_PRICE_WINTER,
    CONF_PRICES,
    CONF_SCAN_INTERVAL_HOURS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_VALID_FROM,
    HISTORY_CHUNK_DAYS,
)

from .test_coordinator import (
    METER,
    FakeRecorder,
    make_coordinator,
    schedule,
    stub_statistics,
)

ZURICH = ZoneInfo("Europe/Zurich")


class ChunkedClient:
    """Answer every request with one reading per day of the requested range."""

    def __init__(self, fail_from: date | None = None) -> None:
        self.calls: list[tuple[date, date]] = []
        self.fail_from = fail_from

    async def async_fetch_readings(self, number, obis, export_type, start, end):
        self.calls.append((start, end))
        if self.fail_from is not None and start >= self.fail_from:
            raise RegioEnergieConnectionError("portal down")
        days = (end - start).days + 1
        return [
            MeterReading(
                datetime.combine(start + timedelta(days=offset), datetime.min.time())
                .replace(hour=12, minute=15, tzinfo=ZURICH)
                .astimezone(UTC),
                1.0,
            )
            for offset in range(days)
        ]


@pytest.fixture
def imported(monkeypatch):
    """Capture what goes to the recorder; the recorder itself starts empty."""
    captured: list[tuple] = []
    monkeypatch.setattr(coordinator_module, "get_instance", lambda hass: FakeRecorder())
    monkeypatch.setattr(
        coordinator_module,
        "async_add_external_statistics",
        lambda hass, metadata, statistics: captured.append((metadata, statistics)),
    )
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    return captured


@pytest.mark.asyncio
async def test_history_is_imported_in_chunks_with_a_continuous_sum(imported) -> None:
    """Oldest chunk first, overlapping days imported once, sums carried over."""
    client = ChunkedClient()
    instance = make_coordinator(client)
    start = datetime.now(ZURICH).date() - timedelta(days=90)

    await instance._async_backfill(METER, start)

    assert client.calls[0][0] == start
    assert all(
        later[0] == earlier[1]
        for earlier, later in zip(client.calls, client.calls[1:], strict=False)
    )
    assert all((end - begin).days <= HISTORY_CHUNK_DAYS for begin, end in client.calls)
    sums = [row["sum"] for _, statistics in imported for row in statistics]
    # One unit a day, every day exactly once: 1, 2, 3, ... without a jump.
    assert sums == [float(n) for n in range(1, len(sums) + 1)]
    assert len(sums) >= 90
    assert instance.backfill_status[METER["statistic_id"]]["state"] == "done"
    assert instance._store.saved["pending_history"] == {}
    assert METER["statistic_id"] in instance._has_history


@pytest.mark.asyncio
async def test_a_failed_history_import_resumes_where_it_stopped(imported) -> None:
    """A portal error marks the chunk to pick up again on the next poll."""
    today = datetime.now(ZURICH).date()
    start = today - timedelta(days=90)
    instance = make_coordinator(ChunkedClient(fail_from=start + timedelta(days=40)))

    await instance._async_backfill(METER, start)

    status = instance.backfill_status[METER["statistic_id"]]
    assert status["state"] == "failed"
    resume_from = (start + timedelta(days=2 * HISTORY_CHUNK_DAYS)).isoformat()
    assert instance._store.saved["pending_history"] == {
        METER["statistic_id"]: resume_from
    }
    assert METER["statistic_id"] not in instance._backfilling
    # The next poll resumes there, even after a restart.
    assert await instance._async_backfill_start(METER) == date.fromisoformat(
        resume_from
    )


@pytest.mark.asyncio
async def test_rebuilding_the_cost_reads_the_stored_consumption(
    imported, monkeypatch
) -> None:
    """Re-pricing applies each month's price to the stored hourly deltas."""
    april = datetime(2026, 4, 30, 21, 0, tzinfo=UTC)  # 23:00 in Zurich
    may = datetime(2026, 4, 30, 22, 0, tzinfo=UTC)  # 00:00 on 1 May
    rows = [
        {"start": april.timestamp(), "sum": 12.0},
        {"start": may.timestamp(), "sum": 15.0},
    ]

    def _statistics(hass, start, end, ids, period, units, types):
        statistic_id = next(iter(ids))
        if end is None and statistic_id.endswith("_import"):
            return {statistic_id: rows}
        if statistic_id.endswith("_import"):
            return {statistic_id: [{"sum": 10.0}]}  # consumption before April 30
        return {statistic_id: [{"sum": 100.0}]}  # cost before April 30

    monkeypatch.setattr(coordinator_module, "statistics_during_period", _statistics)
    meter = {**METER, CONF_PRICES: schedule(("2026-01", 1.0), ("2026-05", 2.0))}
    instance = make_coordinator(None)

    await instance.async_rebuild_cost(meter, april)

    metadata, statistics = imported[0]
    assert metadata["statistic_id"] == "regioenergie_import:water_cost"
    # 2 units in April at 1.0, then 3 units in May at 2.0, on top of 100.
    assert [row["sum"] for row in statistics] == [102.0, 108.0]


def test_migration_turns_hours_into_minutes() -> None:
    """The old default moves to the new one, a custom value is converted."""
    today = date(2026, 10, 20)

    assert migrate_options_v1({}, today)[CONF_SCAN_INTERVAL_MINUTES] == 60
    migrated = migrate_options_v1(
        {CONF_SCAN_INTERVAL_HOURS: 2, CONF_DAYS_TO_FETCH: 7}, today
    )
    assert migrated[CONF_SCAN_INTERVAL_MINUTES] == 120
    assert migrated[CONF_DAYS_TO_FETCH] == 7
    assert CONF_SCAN_INTERVAL_HOURS not in migrated
    assert migrated[CONF_HISTORY_DAYS] == 730


def test_migration_keeps_a_single_price() -> None:
    """A base price becomes one schedule entry."""
    migrated = migrate_options_v1(
        {CONF_METERS: [{**METER, CONF_PRICE: 4.25}]}, date(2026, 10, 20)
    )

    (meter,) = migrated[CONF_METERS]
    assert CONF_PRICE not in meter
    assert meter[CONF_PRICES] == [{CONF_VALID_FROM: "2026-10", CONF_PRICE: 4.25}]


def test_migration_spells_out_the_seasonal_tariff() -> None:
    """Summer and winter price alternate every April and October."""
    from custom_components.regioenergie_import.coordinator import price_for_month

    migrated = migrate_options_v1(
        {CONF_METERS: [{**METER, CONF_PRICE: 0.20, CONF_PRICE_WINTER: 0.30}]},
        date(2026, 10, 20),
    )

    (meter,) = migrated[CONF_METERS]
    prices = meter[CONF_PRICES]
    assert CONF_PRICE_WINTER not in meter
    assert prices[0] == {CONF_VALID_FROM: "2023-01", CONF_PRICE: 0.30}
    assert prices[-1] == {CONF_VALID_FROM: "2026-10", CONF_PRICE: 0.30}
    for month, expected in [
        ("2024-03", 0.30),
        ("2024-04", 0.20),
        ("2025-09", 0.20),
        ("2025-10", 0.30),
        ("2026-06", 0.20),
    ]:
        assert price_for_month(prices, month) == expected


@pytest.mark.asyncio
async def test_polls_read_two_days_and_the_full_window_once_a_day(
    imported, monkeypatch
) -> None:
    """The first poll after 03:00 reads days_to_fetch, the others two days."""
    client = ChunkedClient()
    instance = make_coordinator(client)
    instance._meters = [METER]
    instance._days_to_fetch = 7
    instance._last_correction = None
    instance._failures = {}
    instance.data = None

    async def _no_backfill(meter):
        return None

    async def _no_reprice(meter):
        return False

    instance._async_backfill_start = _no_backfill
    instance._async_reprice_if_needed = _no_reprice

    await instance._async_update_data()
    await instance._async_update_data()

    local_now = datetime.now(ZURICH)
    windows = [(end - start).days for start, end in client.calls]
    if local_now.hour >= 3:
        assert windows == [7, 2]
        assert instance._last_correction == local_now.date()
    else:
        assert windows == [2, 2]
