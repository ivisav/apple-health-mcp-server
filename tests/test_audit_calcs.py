"""Regression tests for the audit fixes: sleep sessions, workout stat columns, duration duplication."""

from collections.abc import Iterator

import duckdb
import pytest

from app.config import settings
from app.services.health import duckdb_queries
from app.services.health.sql_helpers import STAT_VALUE, join_query, value_aggregates


@pytest.fixture
def con(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    monkeypatch.setattr(settings, "DUCKDB_QUERY_CACHE_ENABLED", False)
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE records (type VARCHAR, sourceName VARCHAR, startDate TIMESTAMP, "
        "endDate TIMESTAMP, unit VARCHAR, value DOUBLE, textValue VARCHAR)"
    )
    c.execute(
        "CREATE TABLE workouts (type VARCHAR, duration DOUBLE, durationUnit VARCHAR, "
        "sourceName VARCHAR, startDate TIMESTAMP, endDate TIMESTAMP, creationDate TIMESTAMP)"
    )
    c.execute(
        "CREATE TABLE stats (type VARCHAR, startDate TIMESTAMP, endDate TIMESTAMP, sum DOUBLE, "
        "average DOUBLE, maximum DOUBLE, minimum DOUBLE, unit VARCHAR)"
    )
    monkeypatch.setattr(duckdb_queries, "_get_con", lambda: c)
    yield c
    c.close()


def _sleep(c, start: str, end: str, stage: str) -> None:
    c.execute(
        "INSERT INTO records VALUES ('HKCategoryTypeIdentifierSleepAnalysis','Watch',?,?,NULL,NULL,?)",
        [start, end, stage],
    )


# --- A1 ---------------------------------------------------------------------

def test_a1_night_starting_before_midnight_is_one_bucket(con) -> None:
    deep = "HKCategoryValueSleepAnalysisAsleepDeep"
    core = "HKCategoryValueSleepAnalysisAsleepCore"
    _sleep(con, "2026-10-10 23:30:00", "2026-10-10 23:50:00", deep)  # 20 min before midnight
    _sleep(con, "2026-10-10 23:50:00", "2026-10-11 00:40:00", core)
    _sleep(con, "2026-10-11 00:40:00", "2026-10-11 01:16:00", deep)  # 36 min after midnight
    _sleep(con, "2026-10-11 02:00:00", "2026-10-11 06:30:00", core)  # 44 min gap < 90
    # a separate night (gap > 90 min)
    _sleep(con, "2026-10-11 23:00:00", "2026-10-12 06:00:00", core)
    rows = duckdb_queries.get_sleep_summary_from_duckdb()
    by = {(str(r["night"])[:10], r["stage"]): r for r in rows}
    assert by[("2026-10-11", deep)]["total_minutes"] == 56
    assert by[("2026-10-11", deep)]["segment_count"] == 2
    assert not any(k[0] == "2026-10-10" for k in by)  # nothing left on the pre-midnight day
    assert ("2026-10-12", core) in by
    assert set(rows[0]) == {"night", "stage", "segment_count", "total_minutes"}


# --- A2 ---------------------------------------------------------------------

def test_a2_value_aggregates_picks_average_for_non_cumulative_stats() -> None:
    assert value_aggregates("workouts") == [STAT_VALUE]
    assert "average" in STAT_VALUE and "sum" in STAT_VALUE


def _hike(c) -> None:
    s, e = "2026-10-10 09:00:00", "2026-10-10 12:03:00"
    c.execute("INSERT INTO workouts VALUES ('HKWorkoutActivityTypeHiking',183.4,'min','Watch',?,?,?)", [s, e, e])
    c.execute("INSERT INTO stats VALUES ('HKQuantityTypeIdentifierActiveEnergyBurned',?,?,600,0,0,0,'kcal')", [s, e])
    c.execute("INSERT INTO stats VALUES ('HKQuantityTypeIdentifierDistanceWalkingRunning',?,?,9.1,0,0,0,'km')", [s, e])
    c.execute("INSERT INTO stats VALUES ('HKQuantityTypeIdentifierHeartRate',?,?,0,121,158,84,'count/min')", [s, e])


def test_a2_heart_rate_workout_stat_is_not_zero(con) -> None:
    _hike(con)
    rows = duckdb_queries.get_statistics_by_type_from_duckdb("HKWorkoutActivityTypeHiking")
    hr = next(r for r in rows if r["stat_type"] == "HKQuantityTypeIdentifierHeartRate")
    assert hr["average"] == 121 and hr["max"] == 158 and hr["min"] == 84


# --- A3 ---------------------------------------------------------------------

def test_a3_workout_duration_not_duplicated_by_stats_join(con) -> None:
    _hike(con)  # 3 stat rows would triple-count the duration in the old join
    rows = duckdb_queries.get_statistics_by_type_from_duckdb("HKWorkoutActivityTypeHiking")
    dur = next(r for r in rows if r["stat_type"] == "duration")
    assert dur["sum"] == pytest.approx(183.4) and dur["count"] == 1 and dur["unit"] == "min"
    energy = next(r for r in rows if r["stat_type"] == "HKQuantityTypeIdentifierActiveEnergyBurned")
    assert energy["sum"] == 600 and energy["unit"] == "kcal"


def test_a3_trend_duration_from_workouts_alone(con) -> None:
    _hike(con)
    rows = duckdb_queries.get_trend_data_from_duckdb("HKWorkoutActivityTypeHiking", "month")
    dur = next(r for r in rows if r["stat_type"] == "duration")
    assert dur["sum"] == pytest.approx(183.4) and dur["unit"] == "min"


def test_a3_join_requires_start_and_end_match(con) -> None:
    assert "workouts.endDate = stats.endDate" in join_query
    con.execute("INSERT INTO workouts VALUES ('HKWorkoutActivityTypeWalking',30,'min','W','2026-10-10 09:00:00','2026-10-10 09:30:00','2026-10-10 09:30:00')")
    # same start, different end -> a different (overlapping) workout's stat; must not attach
    con.execute("INSERT INTO stats VALUES ('HKQuantityTypeIdentifierActiveEnergyBurned','2026-10-10 09:00:00','2026-10-10 11:00:00',999,0,0,0,'kcal')")
    rows = duckdb_queries.get_statistics_by_type_from_duckdb("HKWorkoutActivityTypeWalking")
    assert [r["stat_type"] for r in rows] == ["duration"]


def test_a1_date_window_does_not_truncate_edge_night(con) -> None:
    core = "HKCategoryValueSleepAnalysisAsleepCore"
    _sleep(con, "2026-10-10 23:30:00", "2026-10-11 03:30:00", core)  # 240 min, spans midnight
    rows = duckdb_queries.get_sleep_summary_from_duckdb(date_from="2026-10-11")
    assert [(str(r["night"])[:10], r["total_minutes"]) for r in rows] == [("2026-10-11", 240)]
    assert duckdb_queries.get_sleep_summary_from_duckdb(date_to="2026-10-10") == []
