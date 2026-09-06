"""
End-to-end check that source_name filtering tolerates the Unicode punctuation Apple
actually stores in device names: a curly apostrophe (U+2019) and a no-break space
(U+00A0), e.g. ``Igor’s Apple\xa0Watch``. Callers pass a plain ``Igor's Apple Watch``.
"""

from collections.abc import Iterator

import duckdb
import pytest

from app.config import settings
from app.schemas.record import HealthRecordSearchParams
from app.services.health import duckdb_queries

STORED_SOURCE = "Igor’s Apple Watch"  # curly apostrophe + NBSP
QUERIED_SOURCE = "Igor's Apple Watch"  # straight apostrophe + normal space
RECORD_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"

_FUNCS = [
    duckdb_queries.get_statistics_by_type_from_duckdb,
    duckdb_queries.get_trend_data_from_duckdb,
    duckdb_queries.search_health_records_from_duckdb,
]


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
    con.execute(
        "INSERT INTO records VALUES "
        "(?, '1', ?, '', TIMESTAMP '2026-04-01 10:00:00', TIMESTAMP '2026-04-01 10:01:00', "
        "TIMESTAMP '2026-04-01 10:00:30', 'kcal', 12.5, '12.5'), "
        "(?, '1', ?, '', TIMESTAMP '2026-04-02 10:00:00', TIMESTAMP '2026-04-02 10:01:00', "
        "TIMESTAMP '2026-04-02 10:00:30', 'kcal', 7.5, '7.5')",
        [RECORD_TYPE, STORED_SOURCE, RECORD_TYPE, STORED_SOURCE],
    )

    monkeypatch.setattr(settings, "DUCKDB_QUERY_CACHE_ENABLED", False)
    monkeypatch.setattr(duckdb_queries, "_get_con", lambda: con)
    for fn in _FUNCS:
        fn.cache_clear()
    yield con
    for fn in _FUNCS:
        fn.cache_clear()


def test_statistics_matches_plain_ascii_source(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_statistics_by_type_from_duckdb(
        RECORD_TYPE, source_name=QUERIED_SOURCE,
    )
    assert rows and rows[0]["count"] == 2


def test_trend_matches_plain_ascii_source(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_trend_data_from_duckdb(
        RECORD_TYPE, interval="month", source_name=QUERIED_SOURCE,
    )
    assert rows and sum(r["count"] for r in rows) == 2


def test_search_matches_plain_ascii_source(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.search_health_records_from_duckdb(
        HealthRecordSearchParams(
            record_type=RECORD_TYPE, source_name=QUERIED_SOURCE, limit=10,
        ),
    )
    assert len(rows) == 2


def test_wrong_source_still_returns_nothing(seeded_con: duckdb.DuckDBPyConnection) -> None:
    rows = duckdb_queries.get_statistics_by_type_from_duckdb(
        RECORD_TYPE, source_name="Some Other Device",
    )
    assert rows == []
