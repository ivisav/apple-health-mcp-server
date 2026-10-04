"""
Tests for get_blood_pressure_summary_from_duckdb: joins the separately-stored
systolic/diastolic (and optional heart-rate) records into one reading per
timestamp/source, the way Apple Health's HKCorrelationTypeIdentifierBloodPressure
groups them.
"""

from collections.abc import Iterator

import duckdb
import pytest

from app.config import settings
from app.services.health import duckdb_queries

STORED_SOURCE = "Alex’s Apple\xa0Watch"  # curly apostrophe + NBSP
QUERIED_SOURCE = "Alex's Apple Watch"  # straight apostrophe + normal space

SYSTOLIC = "HKQuantityTypeIdentifierBloodPressureSystolic"
DIASTOLIC = "HKQuantityTypeIdentifierBloodPressureDiastolic"
HEART_RATE = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def seeded_con(monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(":memory:")
    con.sql(
        """
        CREATE TABLE records (
            type VARCHAR, sourceVersion VARCHAR, sourceName VARCHAR, device VARCHAR,
            startDate TIMESTAMP, endDate TIMESTAMP, creationDate TIMESTAMP,
            unit VARCHAR, value DOUBLE, textValue VARCHAR
        )
        """,
    )

    def insert(record_type: str, source: str, date: str, value: float) -> None:
        con.execute(
            "INSERT INTO records VALUES "
            "(?, '1', ?, '', ?::TIMESTAMP, ?::TIMESTAMP, ?::TIMESTAMP, 'mmHg', ?, ?)",
            [record_type, source, date, date, date, value, str(value)],
        )

    # Reading 1: systolic + diastolic + heart rate, all same timestamp/source.
    insert(SYSTOLIC, STORED_SOURCE, "2026-04-01 08:00:00", 118.0)
    insert(DIASTOLIC, STORED_SOURCE, "2026-04-01 08:00:00", 76.0)
    insert(HEART_RATE, STORED_SOURCE, "2026-04-01 08:00:00", 62.0)

    # Reading 2: systolic + diastolic only, no heart rate.
    insert(SYSTOLIC, STORED_SOURCE, "2026-04-02 08:00:00", 122.0)
    insert(DIASTOLIC, STORED_SOURCE, "2026-04-02 08:00:00", 80.0)

    # Systolic-only reading (e.g. a bad sync) — should never appear in results.
    insert(SYSTOLIC, STORED_SOURCE, "2026-04-03 08:00:00", 130.0)

    monkeypatch.setattr(settings, "DUCKDB_QUERY_CACHE_ENABLED", False)
    monkeypatch.setattr(duckdb_queries, "_get_con", lambda: con)
    duckdb_queries.get_blood_pressure_summary_from_duckdb.cache_clear()
    yield con
    duckdb_queries.get_blood_pressure_summary_from_duckdb.cache_clear()


def test_systolic_and_diastolic_join_into_one_reading(
    seeded_con: duckdb.DuckDBPyConnection,
) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb()
    reading = next(r for r in rows if str(r["date"]).startswith("2026-04-01"))
    assert reading["systolic"] == 118.0
    assert reading["diastolic"] == 76.0


def test_heart_rate_attached_when_present(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb()
    reading = next(r for r in rows if str(r["date"]).startswith("2026-04-01"))
    assert reading["heart_rate"] == 62.0


def test_heart_rate_absent_when_not_recorded(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb()
    reading = next(r for r in rows if str(r["date"]).startswith("2026-04-02"))
    assert reading["heart_rate"] is None


def test_systolic_only_reading_is_excluded(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb()
    assert all(not str(r["date"]).startswith("2026-04-03") for r in rows)


def test_date_range_filtering(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb(
        date_from="2026-04-02T00:00:00+00:00",
        date_to="2026-04-02T23:59:59+00:00",
    )
    assert len(rows) == 1
    assert str(rows[0]["date"]).startswith("2026-04-02")


def test_source_name_matches_plain_ascii(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb(source_name=QUERIED_SOURCE)
    assert len(rows) == 2


def test_wrong_source_returns_nothing(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_blood_pressure_summary_from_duckdb(source_name="Some Other Device")
    assert rows == []
