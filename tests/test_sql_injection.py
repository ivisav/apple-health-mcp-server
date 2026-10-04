"""
Tool arguments come from an LLM, which can be steered by text it reads (web pages,
notes). None of them may change the shape of a SQL query. Values below are synthetic.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from app.schemas.record import HealthRecordSearchParams
from app.services.health import duckdb_queries as q
from app.services.health.sql_helpers import (
    build_date,
    build_value_range,
    fill_query,
    type_filter,
)

INJECTIONS = [
    "x' OR '1'='1",
    "x'; DROP TABLE records; --",
    "x' OR (SELECT content FROM read_text('/etc/hosts')) <> '' OR 'a'='b",
]


@pytest.fixture
def synthetic_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "synthetic.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE records(type VARCHAR, sourceName VARCHAR, startDate TIMESTAMP, "
        "endDate TIMESTAMP, value DOUBLE, textValue VARCHAR, unit VARCHAR)"
    )
    con.execute(
        "INSERT INTO records VALUES "
        "('HKCategoryTypeIdentifierSleepAnalysis', 'w', '2026-01-01 23:00', '2026-01-02 01:00', "
        "NULL, 'HKCategoryValueSleepAnalysisAsleepDeep', NULL), "
        "('HKQuantityTypeIdentifierBodyMass', 'w', '2026-01-02 08:00', '2026-01-02 08:00', "
        "80, 'O''Brien note', 'kg')"
    )
    con.close()
    monkeypatch.setattr(q.client, "path", path)
    monkeypatch.setattr(q, "_con", None)
    for fn in vars(q).values():  # results are TTL-cached per function
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()
    yield path
    monkeypatch.setattr(q, "_con", None)


# --- shared helpers (SQL text built for DuckDB) ---------------


@pytest.mark.parametrize("bad", INJECTIONS)
def test_type_filter_rejects_non_identifiers(bad: str) -> None:
    with pytest.raises(ValueError, match="record_type"):
        type_filter("records", bad)
    with pytest.raises(ValueError, match="record_type"):
        type_filter("records", ["HKQuantityTypeIdentifierHeartRate", bad])


def test_type_filter_accepts_identifiers() -> None:
    assert type_filter("records", "HKQuantityTypeIdentifierHeartRate") == (
        "records.type = 'HKQuantityTypeIdentifierHeartRate'"
    )
    assert type_filter("records", ["HKA", "HKB"]) == "records.type IN ('HKA', 'HKB')"


@pytest.mark.parametrize("bad", ["2026-01-01' OR '1'='1", "yesterday", "2026-13-40"])
def test_build_date_rejects_non_dates(bad: str) -> None:
    with pytest.raises(ValueError, match="date"):
        build_date(bad, None, "records")
    with pytest.raises(ValueError, match="date"):
        build_date(None, bad, "records")


def test_build_date_accepts_iso_dates_and_timestamps() -> None:
    assert build_date("2026-01-01", "2026-01-31T23:59:59", "records") == (
        "records.startDate >= '2026-01-01' and records.startDate <= '2026-01-31T23:59:59'"
    )
    assert build_date("2026-01-01 10:00:00", None, "records") == (
        "records.startDate >= '2026-01-01T10:00:00'"
    )


@pytest.mark.parametrize("bad", ["5' OR '1'='1", "ten", "nan"])
def test_build_value_range_rejects_non_numbers(bad: str) -> None:
    with pytest.raises(ValueError, match="number"):
        build_value_range(bad, None, "value")


def test_build_value_range_emits_numbers() -> None:
    assert build_value_range("1.5", "10", "value") == "value >= 1.5 and value <= 10.0"


def test_fill_query_uses_workout_duration_bounds_and_spaces_order_by() -> None:
    query = fill_query(
        HealthRecordSearchParams(
            record_type="HKWorkoutActivityTypeCycling",
            min_workout_duration="30",
            value_min="100",
        )
    )
    assert "duration >= 30.0" in query
    assert "sum >= 100.0" in query
    assert " ORDER BY " in query or "ORDER BY workouts" in query.replace("\n", " ")


# --- DuckDB queries end to end on a synthetic DB ---------------------------------


@pytest.mark.parametrize("bad", INJECTIONS)
def test_search_values_treats_input_as_a_literal(synthetic_db: Path, bad: str) -> None:
    assert q.search_values_from_duckdb(None, bad) == []


def test_search_values_matches_apostrophes_literally(synthetic_db: Path) -> None:
    rows = q.search_values_from_duckdb(None, "O'Brien note")
    assert [r["textValue"] for r in rows] == ["O'Brien note"]


def test_search_values_rejects_injected_record_type_and_dates(synthetic_db: Path) -> None:
    with pytest.raises(ValueError, match="record_type"):
        q.search_values_from_duckdb(INJECTIONS[0], "x")
    with pytest.raises(ValueError, match="date"):
        q.search_values_from_duckdb(None, "x", date_from=INJECTIONS[0])


def test_sleep_summary_rejects_injected_dates(synthetic_db: Path) -> None:
    with pytest.raises(ValueError, match="date"):
        q.get_sleep_summary_from_duckdb(date_from="2026-01-01' OR '1'='1")
    assert q.get_sleep_summary_from_duckdb(date_from="2026-01-01")[0]["segment_count"] == 1
