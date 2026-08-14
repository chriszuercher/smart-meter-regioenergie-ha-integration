"""Tests for the hourly aggregation and the statistics import."""

from datetime import date, datetime, timedelta
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
    CONF_PRICE_WINTER,
    CONF_STATISTIC_ID,
    CONF_UNIT,
)
from custom_components.regioenergie_import.coordinator import (
    RegioEnergieCoordinator,
    build_hourly_totals,
    build_metadata,
    cost_statistic_id_for,
    drop_unpublished_tail,
    price_at,
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
    return instance


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
    """Repeated local times (autumn DST) keep their total consumption."""
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
    """Without a winter price the base price holds in every month."""
    meter = {**METER, CONF_PRICE: 2.5}

    assert price_at(meter, datetime(2026, 1, 15, tzinfo=ZURICH)) == 2.5
    assert price_at(meter, datetime(2026, 7, 15, tzinfo=ZURICH)) == 2.5


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        (3, 0.30),  # March still winter
        (4, 0.20),  # April starts the summer rate
        (9, 0.20),  # September is the last summer month
        (10, 0.30),  # October back to winter
    ],
)
def test_seasonal_price_switches_in_april_and_october(
    month: int, expected: float
) -> None:
    """Summer runs April to September, the rest of the year is dearer."""
    meter = {**METER, CONF_PRICE: 0.20, CONF_PRICE_WINTER: 0.30}

    assert price_at(meter, datetime(2026, month, 15, tzinfo=ZURICH)) == expected


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
        CONF_PRICE: 2.0,
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
    meter = {**METER, CONF_PRICE: 0.20, CONF_PRICE_WINTER: 0.30}
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
    meter = {**METER, CONF_PRICE: 2.0}
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
