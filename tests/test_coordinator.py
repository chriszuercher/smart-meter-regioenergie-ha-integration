"""Tests for the hourly aggregation and the statistics import."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.regioenergie_import import coordinator as coordinator_module
from custom_components.regioenergie_import.api import MeterReading, parse_csv
from custom_components.regioenergie_import.const import (
    CONF_COST_STATISTIC_ID,
    CONF_EXPORT_TYPE,
    CONF_METER_NUMBER,
    CONF_OBIS_CODE,
    CONF_PRICE,
    CONF_PRICES,
    CONF_STATISTIC_ID,
    CONF_UNIT,
    CONF_VALID_FROM,
)
from custom_components.regioenergie_import.coordinator import (
    RegioEnergieCoordinator,
    build_hourly_totals,
    build_metadata,
    cost_statistic_id_for,
    drop_unpublished_tail,
    first_repriced_month,
    price_at,
    price_for_month,
)

ZURICH = ZoneInfo("Europe/Zurich")

# Fixed "current time" so the tests do not depend on the wall clock.
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=ZURICH)

METER = {
    "name": "Water",
    CONF_METER_NUMBER: "CH1000000000000000000000000012345",
    CONF_OBIS_CODE: "1-1:1.29.0",
    CONF_EXPORT_TYPE: "WirkBezug",
    CONF_UNIT: "m³",
    CONF_STATISTIC_ID: "regioenergie_import:water_import",
}


def reading(
    hour: int, minute: int, value: float, day: int = 13, empty: bool = False
) -> MeterReading:
    """Build a reading at a Swiss local time."""
    stamp = datetime(2026, 8, day, hour, minute, tzinfo=ZURICH)
    return MeterReading(stamp, value, empty)


class FakeClient:
    """Return a fixed set of readings and record how it was called."""

    def __init__(self, readings: list[MeterReading]) -> None:
        self.readings = readings
        self.calls: list[tuple] = []

    async def async_fetch_readings(self, number, obis, export_type, start, end):
        self.calls.append((number, obis, export_type, start, end))
        return self.readings


class FakeStore:
    """Keep the saved import state in memory."""

    def __init__(self) -> None:
        self.saved = None

    async def async_save(self, data) -> None:
        self.saved = data


class FakeRecorder:
    """Run recorder jobs inline instead of on the recorder thread."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def imported(monkeypatch):
    """Capture what the coordinator hands to the recorder."""
    captured: list[tuple] = []

    monkeypatch.setattr(coordinator_module, "get_instance", lambda hass: FakeRecorder())
    monkeypatch.setattr(
        coordinator_module,
        "async_add_external_statistics",
        lambda hass, metadata, statistics: captured.append((metadata, statistics)),
    )
    return captured


class FakeHass:
    """Just enough of hass for the import: the configured currency."""

    class config:
        """Stand-in for hass.config."""

        currency = "CHF"


def make_coordinator(client) -> RegioEnergieCoordinator:
    """Build a coordinator without going through Home Assistant's setup."""
    instance = RegioEnergieCoordinator.__new__(RegioEnergieCoordinator)
    instance.hass = FakeHass()
    instance.client = client
    instance._timezone = ZURICH
    instance._has_history = set()
    instance._backfilling = set()
    instance._state = {"prices": {}, "pending_history": {}}
    instance._store = FakeStore()
    instance.backfill_status = {}
    return instance


def schedule(*entries: tuple[str, float]) -> list[dict]:
    """Build a price schedule from (month, price) pairs."""
    return [{CONF_VALID_FROM: month, CONF_PRICE: price} for month, price in entries]


def stub_statistics(rows_by_window):
    """Return a statistics_during_period stub driven by the requested window."""

    def _statistics_during_period(hass, start, end, ids, period, units, types):
        rows = rows_by_window.get(end - start)
        return {next(iter(ids)): rows} if rows else {}

    return _statistics_during_period


# --- hourly grouping -------------------------------------------------------


def test_interval_end_timestamps_land_on_their_own_hour() -> None:
    """01:15 through 02:00 belong to the hour 01:00, not to two hours."""
    totals = build_hourly_totals(
        [
            reading(1, 15, 1.0),
            reading(1, 30, 2.0),
            reading(1, 45, 3.0),
            reading(2, 0, 4.0),
        ]
    )

    assert totals == {datetime(2026, 8, 13, 1, 0, tzinfo=ZURICH): 10.0}


def test_hourly_grouping_splits_consecutive_hours() -> None:
    """The next quarter hour starts the next bucket."""
    totals = build_hourly_totals([reading(2, 0, 4.0), reading(2, 15, 5.0)])

    assert totals == {
        datetime(2026, 8, 13, 1, 0, tzinfo=ZURICH): 4.0,
        datetime(2026, 8, 13, 2, 0, tzinfo=ZURICH): 5.0,
    }


def test_hourly_grouping_works_for_hourly_exports() -> None:
    """A 60 minute interval ending at 02:00 also belongs to the hour 01:00."""
    totals = build_hourly_totals([reading(2, 0, 4.0)])

    assert totals == {datetime(2026, 8, 13, 1, 0, tzinfo=ZURICH): 4.0}


def test_duplicate_timestamps_are_added_up() -> None:
    """Two rows for the very same quarter hour keep their total consumption."""
    totals = build_hourly_totals([reading(2, 15, 1.0), reading(2, 15, 2.0)])

    assert totals == {datetime(2026, 8, 13, 2, 0, tzinfo=ZURICH): 3.0}


def test_unpublished_tail_is_dropped() -> None:
    """The empty padding at the end of an export is not consumption of zero."""
    kept = drop_unpublished_tail(
        [
            reading(1, 15, 1.0),
            reading(1, 30, 0.0, empty=True),
            reading(1, 45, 2.0),
            reading(2, 0, 0.0, empty=True),
            reading(2, 15, 0.0, empty=True),
        ]
    )

    # The gap in the middle stays, the padding at the end goes.
    assert [(r.timestamp.hour, r.timestamp.minute) for r in kept] == [
        (1, 15),
        (1, 30),
        (1, 45),
    ]


def test_an_entirely_unpublished_export_drops_everything() -> None:
    """A meter without any data yet imports nothing."""
    assert drop_unpublished_tail([reading(1, 15, 0.0, empty=True)]) == []


def test_real_export_format_parses_end_to_end() -> None:
    """The exact CSV shape of okp.regioenergie.ch aggregates correctly."""
    csv = (
        "Datum;Zeit;Wert;\n"
        "06.08.2026;02:15;0.07;\n"
        "06.08.2026;02:30;0.089;\n"
        "06.08.2026;02:45;0.072;\n"
        "06.08.2026;03:00;0.082;\n"
        "06.08.2026;03:15;;\n"
        "06.08.2026;03:30;;\n"
    )

    totals = build_hourly_totals(drop_unpublished_tail(parse_csv(csv)))

    # Trailing semicolons do not confuse the parser, the four quarters land on
    # the hour 02:00, and the empty tail is left out.
    assert totals == {datetime(2026, 8, 6, 2, 0, tzinfo=ZURICH): pytest.approx(0.313)}


# --- statistics import -----------------------------------------------------


@pytest.mark.asyncio
async def test_import_builds_a_running_total(imported, monkeypatch) -> None:
    """Hourly values are accumulated into the sum the energy dashboard reads."""
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    instance = make_coordinator(
        FakeClient([reading(1, 15, 1.0), reading(2, 0, 2.0), reading(2, 15, 4.0)])
    )

    result = await instance._async_import_meter(
        METER, date(2026, 8, 6), date(2026, 8, 13), NOW
    )

    (metadata, statistics) = imported[0]
    assert [row["start"] for row in statistics] == [
        datetime(2026, 8, 13, 1, 0, tzinfo=ZURICH),
        datetime(2026, 8, 13, 2, 0, tzinfo=ZURICH),
    ]
    # 1.0 + 2.0 in the first hour, then 4.0 on top of it.
    assert [row["sum"] for row in statistics] == [3.0, 7.0]
    assert metadata["statistic_id"] == "regioenergie_import:water_import"
    assert metadata["unit_of_measurement"] == "m³"
    assert result == {
        "statistic_id": "regioenergie_import:water_import",
        "imported": 2,
        "last_sum": 7.0,
        "last_hour": "2026-08-13T02:00:00+02:00",
        "last_cost_sum": None,
    }
    # No price configured, so no cost statistic was written.
    assert len(imported) == 1


@pytest.mark.asyncio
async def test_import_continues_the_existing_total(imported, monkeypatch) -> None:
    """The running total picks up where the last stored statistic left off."""
    monkeypatch.setattr(
        coordinator_module,
        "statistics_during_period",
        stub_statistics({timedelta(hours=1): [{"sum": 100.0}]}),
    )
    instance = make_coordinator(FakeClient([reading(1, 15, 1.5)]))

    await instance._async_import_meter(METER, date(2026, 8, 6), date(2026, 8, 13), NOW)

    assert [row["sum"] for row in imported[0][1]] == [101.5]


@pytest.mark.asyncio
async def test_baseline_widens_the_lookback_over_gaps(imported, monkeypatch) -> None:
    """A gap right before the window does not restart the total at zero."""
    monkeypatch.setattr(
        coordinator_module,
        "statistics_during_period",
        stub_statistics({timedelta(days=30): [{"sum": 10.0}, {"sum": 42.0}]}),
    )
    instance = make_coordinator(FakeClient([reading(1, 15, 1.0)]))

    await instance._async_import_meter(METER, date(2026, 8, 6), date(2026, 8, 13), NOW)

    # The most recent row before the window is the one that counts.
    assert [row["sum"] for row in imported[0][1]] == [43.0]


@pytest.mark.asyncio
async def test_the_running_hour_is_left_for_the_next_poll(
    imported, monkeypatch
) -> None:
    """An hour that has not elapsed yet would be imported short."""
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    # NOW is 12:00, so the readings of the hour 12:00 are still incomplete.
    instance = make_coordinator(
        FakeClient([reading(11, 45, 1.0), reading(12, 0, 2.0), reading(12, 15, 3.0)])
    )

    result = await instance._async_import_meter(
        METER, date(2026, 8, 6), date(2026, 8, 13), NOW
    )

    assert [row["start"] for row in imported[0][1]] == [
        datetime(2026, 8, 13, 11, 0, tzinfo=ZURICH)
    ]
    assert result["imported"] == 1


@pytest.mark.asyncio
async def test_import_without_readings_pushes_nothing(imported, monkeypatch) -> None:
    """An empty export is not an error, it simply imports nothing."""
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    instance = make_coordinator(FakeClient([]))

    result = await instance._async_import_meter(
        METER, date(2026, 8, 6), date(2026, 8, 13), NOW
    )

    assert imported == []
    assert result["imported"] == 0


# --- costs -----------------------------------------------------------------


def test_cost_statistic_id_sits_next_to_the_consumption_one() -> None:
    """The cost statistic is derived from the consumption statistic."""
    from homeassistant.components.recorder.statistics import valid_statistic_id

    cost_id = cost_statistic_id_for("regioenergie_import:strom_import")

    assert cost_id == "regioenergie_import:strom_cost"
    assert valid_statistic_id(cost_id)


def test_a_meter_without_a_price_has_no_cost() -> None:
    """Prices are optional."""
    assert price_at(METER, NOW) is None


def test_a_single_price_applies_all_year() -> None:
    """One entry prices every month, before and after it."""
    meter = {**METER, CONF_PRICES: schedule(("2026-06", 2.5))}

    assert price_at(meter, datetime(2024, 1, 15, tzinfo=ZURICH)) == 2.5
    assert price_at(meter, datetime(2026, 7, 15, tzinfo=ZURICH)) == 2.5


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        ("2025-12", 0.25),  # before the first entry: the first price
        ("2026-01", 0.25),
        ("2026-02", 0.25),  # carried over from January
        ("2026-04", 0.25),
        ("2026-05", 0.30),
        ("2027-03", 0.30),  # still the last entry
    ],
)
def test_prices_carry_over_until_the_next_entry(month: str, expected: float) -> None:
    """A price holds from its month until the month of the next entry."""
    prices = schedule(("2026-05", 0.30), ("2026-01", 0.25))

    assert price_for_month(prices, month) == expected


def test_the_month_is_taken_in_swiss_time() -> None:
    """The first hour of May in Zurich is still April in UTC."""
    meter = {**METER, CONF_PRICES: schedule(("2026-04", 1.0), ("2026-05", 2.0))}

    assert price_at(meter, datetime(2026, 4, 30, 22, 0, tzinfo=UTC)) == 2.0


def test_unchanged_prices_need_no_repricing() -> None:
    """Same schedule, nothing to do."""
    prices = schedule(("2026-01", 0.25))

    assert first_repriced_month(prices, list(prices)) is None


def test_a_new_entry_reprices_from_its_month() -> None:
    """Adding a price for May changes the months from May on."""
    old = schedule(("2026-01", 0.25))
    new = schedule(("2026-01", 0.25), ("2026-05", 0.30))

    assert first_repriced_month(old, new) == "2026-05"


def test_a_changed_first_price_reprices_everything() -> None:
    """The first price also covers the months before it."""
    old = schedule(("2026-01", 0.25))
    new = schedule(("2026-01", 0.27))

    assert first_repriced_month(old, new) == "0000-01"


def test_a_redundant_entry_reprices_nothing() -> None:
    """An entry that repeats the price before it changes no month."""
    old = schedule(("2026-01", 0.25))
    new = schedule(("2026-01", 0.25), ("2026-03", 0.25))

    assert first_repriced_month(old, new) is None


def test_a_first_price_prices_the_whole_history() -> None:
    """A meter without prices so far gets its cost from the beginning."""
    assert first_repriced_month([], schedule(("2026-01", 0.25))) == "0000-01"
    assert first_repriced_month(schedule(("2026-01", 0.25)), []) is None


@pytest.mark.asyncio
async def test_cost_statistic_is_imported_next_to_consumption(
    imported, monkeypatch
) -> None:
    """A priced meter yields a second statistic with the running cost."""
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    meter = {
        **METER,
        CONF_PRICES: schedule(("2026-01", 2.0)),
        CONF_COST_STATISTIC_ID: "regioenergie_import:water_cost",
    }
    instance = make_coordinator(FakeClient([reading(1, 15, 1.5), reading(2, 15, 2.5)]))

    result = await instance._async_import_meter(
        meter, date(2026, 8, 6), date(2026, 8, 13), NOW
    )

    consumption_meta, consumption = imported[0]
    cost_meta, cost = imported[1]
    assert [row["sum"] for row in consumption] == [1.5, 4.0]
    # Same hours, multiplied by the price and accumulated the same way.
    assert [row["start"] for row in cost] == [row["start"] for row in consumption]
    assert [row["sum"] for row in cost] == [3.0, 8.0]
    assert cost_meta["statistic_id"] == "regioenergie_import:water_cost"
    assert cost_meta["has_sum"] is True
    # The cost carries the currency, the consumption the meter's own unit.
    assert cost_meta["unit_of_measurement"] == "CHF"
    assert consumption_meta["unit_of_measurement"] == "m³"
    assert result["last_cost_sum"] == 8.0


@pytest.mark.asyncio
async def test_cost_uses_the_price_of_each_hour(imported, monkeypatch) -> None:
    """A backfill across the tariff switch is priced month by month."""
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", stub_statistics({})
    )
    meter = {**METER, CONF_PRICES: schedule(("2026-04", 0.20), ("2026-10", 0.30))}
    instance = make_coordinator(
        FakeClient(
            [
                MeterReading(datetime(2026, 9, 30, 23, 45, tzinfo=ZURICH), 10.0),
                MeterReading(datetime(2026, 10, 1, 1, 0, tzinfo=ZURICH), 10.0),
            ]
        )
    )

    await instance._async_import_meter(
        meter,
        date(2026, 9, 1),
        date(2026, 10, 2),
        datetime(2026, 10, 2, 12, 0, tzinfo=ZURICH),
    )

    # 10 units in September at 0.20, then 10 in October at 0.30.
    assert [row["sum"] for row in imported[1][1]] == [2.0, 5.0]


@pytest.mark.asyncio
async def test_cost_continues_its_own_running_total(imported, monkeypatch) -> None:
    """The cost statistic keeps its own baseline, not the consumption one."""
    windows = {timedelta(hours=1): [{"sum": 100.0}]}
    monkeypatch.setattr(
        coordinator_module,
        "statistics_during_period",
        lambda hass, start, end, ids, period, units, types: (
            {next(iter(ids)): windows[end - start]}
            if end - start in windows and "cost" in next(iter(ids))
            else {}
        ),
    )
    meter = {**METER, CONF_PRICES: schedule(("2026-01", 2.0))}
    instance = make_coordinator(FakeClient([reading(1, 15, 1.5)]))

    await instance._async_import_meter(meter, date(2026, 8, 6), date(2026, 8, 13), NOW)

    assert [row["sum"] for row in imported[0][1]] == [1.5]  # consumption from 0
    assert [row["sum"] for row in imported[1][1]] == [103.0]  # cost from 100


# --- metadata --------------------------------------------------------------


@pytest.mark.parametrize("unit", ["kWh", "MWh", "Wh", "GJ", "m³", "L"])
def test_metadata_is_accepted_by_the_recorder_schema(unit: str) -> None:
    """Every offered unit produces metadata the recorder can store."""
    from homeassistant.components.recorder.db_schema import StatisticsMeta

    metadata = build_metadata("regioenergie_import:water_import", "Water", unit)

    assert metadata["has_sum"] is True
    assert metadata["source"] == "regioenergie_import"
    # Raises TypeError if a key is not a column of the statistics metadata.
    StatisticsMeta(**metadata)


# --- daylight saving time --------------------------------------------------


def quarter_hours(day: str, times: list[str], value: float = 1.0) -> str:
    """Build an export with one row per given local time."""
    return "Datum;Zeit;Wert;\n" + "".join(f"{day};{t};{value};\n" for t in times)


def all_quarter_hours(hours: list[int]) -> list[str]:
    """Return the interval end times of whole local hours, as the export has them."""
    times = []
    for hour in hours:
        times += [f"{hour:02d}:15", f"{hour:02d}:30", f"{hour:02d}:45"]
        times.append(f"{hour + 1:02d}:00")
    return times


def test_the_repeated_autumn_hour_keeps_both_passes() -> None:
    """On 25 Oct 2026 the hour after 02:00 runs twice; both are their own hour."""
    # 01:00-02:00, then 02:00-03:00 in summer time, the same wall times again
    # in winter time, then 03:00-04:00.
    times = (
        all_quarter_hours([1])
        + all_quarter_hours([2])[:3]
        + ["02:00"]  # 03:00 summer time is 02:00 winter time
        + all_quarter_hours([2])[:3]
        + ["03:00"]
        + all_quarter_hours([3])
    )
    readings = parse_csv(quarter_hours("25.10.2026", times))

    totals = build_hourly_totals(readings)

    assert len({r.timestamp for r in readings}) == len(readings) == 16
    assert sorted(totals) == [
        datetime(2026, 10, 24, 23, 0, tzinfo=UTC),  # 01:00 summer time
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC),  # 02:00 summer time
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),  # 02:00 winter time
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),  # 03:00 winter time
    ]
    assert sum(totals.values()) == 16.0
    assert set(totals.values()) == {4.0}


def test_the_skipped_spring_hour_does_not_collide() -> None:
    """On 29 Mar 2026 02:00 does not exist; 03:00 ends the hour 01:00."""
    times = [*all_quarter_hours([0, 1])[:-1], "03:00", *all_quarter_hours([3])]
    readings = parse_csv(quarter_hours("29.03.2026", times))

    totals = build_hourly_totals(readings)

    assert sorted(totals) == [
        datetime(2026, 3, 28, 23, 0, tzinfo=UTC),  # 00:00 winter time
        datetime(2026, 3, 29, 0, 0, tzinfo=UTC),  # 01:00 winter time
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC),  # 03:00 summer time
    ]
    assert set(totals.values()) == {4.0}


def test_readings_are_stored_in_utc() -> None:
    """Every timestamp leaves the parser in UTC."""
    (reading_,) = parse_csv("Datum;Zeit;Wert\n13.08.2026;12:15;1.0\n")

    assert reading_.timestamp.tzinfo is UTC
    assert reading_.timestamp == datetime(2026, 8, 13, 10, 15, tzinfo=UTC)
