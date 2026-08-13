"""Tests for the hourly aggregation and the statistics import."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.regioenergie_import import coordinator as coordinator_module
from custom_components.regioenergie_import.api import MeterReading, parse_csv
from custom_components.regioenergie_import.const import (
    CONF_EXPORT_TYPE,
    CONF_METER_NUMBER,
    CONF_OBIS_CODE,
    CONF_STATISTIC_ID,
    CONF_UNIT,
)
from custom_components.regioenergie_import.coordinator import (
    RegioEnergieCoordinator,
    build_hourly_totals,
    build_metadata,
    drop_unpublished_tail,
)

ZURICH = ZoneInfo("Europe/Zurich")

# Fixed "current time" so the tests do not depend on the wall clock.
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=ZURICH)

METER = {
    "name": "Water",
    CONF_METER_NUMBER: "CH8000051234500000000000000014186",
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


def make_coordinator(client) -> RegioEnergieCoordinator:
    """Build a coordinator without going through Home Assistant's setup."""
    instance = RegioEnergieCoordinator.__new__(RegioEnergieCoordinator)
    instance.hass = object()
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
    }


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
