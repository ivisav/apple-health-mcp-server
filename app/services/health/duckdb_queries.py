import logging
import math
from typing import Any

import duckdb

from app.config import settings
from app.schemas.record import (
    HealthRecordSearchParams,
    IntervalType,
    RecordType,
    WorkoutType,
)
from app.services.duckdb_client import DuckDBClient
from app.services.health.sql_helpers import (
    fill_query,
    get_table,
    STAT_MAX,
    STAT_MIN,
    STAT_VALUE,
    join_query,
    join_string,
    normalized_source,
    sql_timestamp,
    sql_type,
    type_filter,
    value_aggregates,
)
from app.utils.cache import ttl_cache

client = DuckDBClient()
_con = None


def _cache_enabled() -> bool:
    return settings.DUCKDB_QUERY_CACHE_ENABLED


_default_cache = {
    "maxsize": settings.DUCKDB_QUERY_CACHE_MAXSIZE,
    "ttl_seconds": settings.DUCKDB_QUERY_CACHE_TTL_SECONDS,
    "enabled": _cache_enabled,
}


def _get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect(client.path, read_only=True)
    return _con


def _source_name_filter(source_name: str | None) -> str:
    """
    ``AND <norm(sourceName)> = <norm($source_name)>`` (bound param), or ``""``.

    Both sides go through ``normalized_source`` so a plain ``Alex's Apple Watch``
    matches the ``Alex’s Apple\xa0Watch`` (curly apostrophe + NBSP) Apple stores.
    """
    if not source_name:
        return ""
    return f"AND {normalized_source('sourceName')} = {normalized_source('$source_name')}"


@ttl_cache(**_default_cache)
def get_health_summary_from_duckdb() -> list[dict[str, Any]]:
    records = _get_con().sql(
        """SELECT type, COUNT(*) AS count FROM records
        GROUP BY type ORDER BY count DESC""",
    )
    workouts = _get_con().sql(
        f"""SELECT workouts.type, COUNT(*) AS count FROM workouts {join_query}
        GROUP BY workouts.type ORDER BY count DESC""",
    )

    return client.format_response([records, workouts])


@ttl_cache(**_default_cache)
def search_health_records_from_duckdb(
    params: HealthRecordSearchParams,
) -> list[dict[str, Any]]:
    query: str = "SELECT * FROM"
    query += fill_query(params)
    response = _get_con().sql(query)
    return client.format_response(response)


@ttl_cache(**_default_cache)
def get_statistics_by_type_from_duckdb(
    record_type: RecordType | WorkoutType | list[str] | str,
    source_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    table = get_table(record_type)
    complimentary_table = "stats" if table == "workouts" else "records"
    join_clause = join_string(table)
    values = value_aggregates(table)
    results = []

    query_params: dict[str, Any] = {}
    if source_name:
        query_params["source_name"] = source_name
    if date_from:
        query_params["date_from"] = date_from
    if date_to:
        query_params["date_to"] = date_to

    src_filter = _source_name_filter(source_name)

    if table == "workouts":
        # Duration comes from the workouts table alone: joining to stats first would repeat
        # each workout once per stat row (and label minutes with the stat's unit).
        results.append(
            _get_con().sql(
                f"""
                    SELECT workouts.type, 'duration' AS stat_type, COUNT(*) AS count,
                    AVG(duration) AS average, SUM(duration) AS sum, MIN(duration) AS min,
                    MAX(duration) AS max, durationUnit AS unit FROM workouts
                    WHERE {type_filter(table, record_type)}
                    {src_filter}
                    {f"AND workouts.startDate >= $date_from" if date_from else ""}
                    {f"AND workouts.startDate <= $date_to" if date_to else ""}
                    GROUP BY workouts.type, durationUnit
                    """,
                params=query_params,
            ),
        )
        results.append(
            _get_con().sql(
                f"""
                    SELECT workouts.type, stats.type AS stat_type, COUNT(*) AS count,
                    AVG({STAT_VALUE}) AS average,
                    SUM(stats.sum) AS sum, MIN({STAT_MIN}) AS min,
                    MAX({STAT_MAX}) AS max, unit FROM workouts {join_clause}
                    WHERE {type_filter(table, record_type)}
                    {src_filter}
                    {f"AND workouts.startDate >= $date_from" if date_from else ""}
                    {f"AND workouts.startDate <= $date_to" if date_to else ""}
                    GROUP BY workouts.type, stats.type, unit
                    """,
                params=query_params,
            ),
        )
        return client.format_response(results)

    value = values[0]
    for value in values:
        results.append(
            _get_con().sql(
                f"""
                    SELECT {table}.type, {complimentary_table}.type AS stat_type, COUNT(*) AS count,
                    AVG({value}) AS average, SUM({value}) AS sum, MIN({value}) AS min,
                    MAX({value}) AS max, unit FROM {table} {join_clause}
                    WHERE {type_filter(table, record_type)}
                    {src_filter}
                    {f"AND {table}.startDate >= $date_from" if date_from else ""}
                    {f"AND {table}.startDate <= $date_to" if date_to else ""}
                    GROUP BY {table}.type,
                    {complimentary_table}.type, unit
                    """,
                params=query_params,
            ),
        )
    return client.format_response(results)


@ttl_cache(**_default_cache)
def get_trend_data_from_duckdb(
    record_type: RecordType | WorkoutType | list[str] | str,
    interval: IntervalType = "month",
    date_from: str | None = None,
    date_to: str | None = None,
    source_name: str | None = None,
) -> list[dict[str, Any]]:
    table = get_table(record_type)
    join_clause = join_string(table)
    values = value_aggregates(table)
    results = []

    query_params: dict[str, Any] = {}
    if source_name:
        query_params["source_name"] = source_name
    if date_from:
        query_params["date_from"] = date_from
    if date_to:
        query_params["date_to"] = date_to

    src_filter = _source_name_filter(source_name)

    if table == "workouts":
        results.append(
            _get_con().sql(
                f"""
            SELECT workouts.type, 'duration' AS stat_type, sourceName,
            time_bucket(INTERVAL '1 {interval}', workouts.startDate) AS interval,
            AVG(duration) AS average, SUM(duration) AS sum,
            MIN(duration) AS min, MAX(duration) AS max, COUNT(*) AS count,
            durationUnit AS unit FROM workouts
            WHERE {type_filter(table, record_type)}
            {src_filter}
            {f"AND workouts.startDate >= $date_from" if date_from else ""}
            {f"AND workouts.startDate <= $date_to" if date_to else ""}
            GROUP BY interval, workouts.type, sourceName, durationUnit ORDER BY interval ASC
        """,
                params=query_params,
            ),
        )
        results.append(
            _get_con().sql(
                f"""
            SELECT workouts.type, stats.type AS stat_type, sourceName,
            time_bucket(INTERVAL '1 {interval}', workouts.startDate) AS interval,
            AVG({STAT_VALUE}) AS average, SUM(stats.sum) AS sum,
            MIN({STAT_MIN}) AS min, MAX({STAT_MAX}) AS max, COUNT(*) AS count,
            unit FROM workouts {join_clause}
            WHERE {type_filter(table, record_type)}
            {src_filter}
            {f"AND workouts.startDate >= $date_from" if date_from else ""}
            {f"AND workouts.startDate <= $date_to" if date_to else ""}
            GROUP BY interval, workouts.type, stats.type, sourceName, unit ORDER BY interval ASC
        """,
                params=query_params,
            ),
        )
        return client.format_response(results)

    for value in values:
        results.append(
            _get_con().sql(
                f"""
            SELECT {table}.type, sourceName, time_bucket(INTERVAL '1 {interval}',
            {table}.startDate) AS interval,
            AVG({value}) AS average, SUM({value}) AS sum,
            MIN({value}) AS min, MAX({value}) AS max, COUNT(*) AS count,
            unit FROM {table} {join_clause}
            WHERE {type_filter(table, record_type)}
            {src_filter}
            {f"AND {table}.startDate >= $date_from" if date_from else ""}
            {f"AND {table}.startDate <= $date_to" if date_to else ""}
            GROUP BY interval, {table}.type, sourceName, unit ORDER BY interval ASC
        """,
                params=query_params,
            ),
        )
    return client.format_response(results)


@ttl_cache(**_default_cache)
def get_sleep_summary_from_duckdb(
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if date_from:
        params["date_from"] = sql_timestamp(date_from, "date_from")
    if date_to:
        params["date_to"] = sql_timestamp(date_to, "date_to")
    # The segment filter is padded by a day each side so sessions at the window edges are
    # complete; the window is then applied to the wake date.
    # Segments are chained into sessions (a gap > 90 min starts a new one) and each session
    # is labelled by the day it ENDS (wake date), so a night that begins before midnight is
    # not split across two calendar days.
    result = _get_con().sql(
        f"""
        WITH seg AS (
            SELECT startDate, endDate, textValue AS stage FROM records
            WHERE type = 'HKCategoryTypeIdentifierSleepAnalysis'
            {"AND startDate >= CAST($date_from AS TIMESTAMP) - INTERVAL 1 DAY" if date_from else ""}
            {"AND startDate <= CAST($date_to AS TIMESTAMP) + INTERVAL 1 DAY" if date_to else ""}
        ),
        ordered AS (
            SELECT *, MAX(endDate) OVER (
                ORDER BY startDate, endDate ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prev_end FROM seg
        ),
        sess AS (
            SELECT *, SUM(CASE WHEN prev_end IS NULL
                    OR date_diff('minute', prev_end, startDate) > 90 THEN 1 ELSE 0 END)
                OVER (ORDER BY startDate, endDate) AS sid FROM ordered
        ),
        wake AS (SELECT sid, DATE_TRUNC('day', MAX(endDate)) AS night FROM sess GROUP BY sid)
        SELECT wake.night AS night, sess.stage AS stage,
        COUNT(*) AS segment_count,
        SUM(date_diff('minute', sess.startDate, sess.endDate)) AS total_minutes
        FROM sess JOIN wake USING (sid)
        WHERE TRUE
        {"AND wake.night >= DATE_TRUNC('day', CAST($date_from AS TIMESTAMP))" if date_from else ""}
        {"AND wake.night <= CAST($date_to AS TIMESTAMP)" if date_to else ""}
        GROUP BY wake.night, sess.stage ORDER BY wake.night DESC, sess.stage
        """,
        params=params,
    )
    return client.format_response(result)


@ttl_cache(**_default_cache)
def get_blood_pressure_summary_from_duckdb(
    date_from: str | None = None,
    date_to: str | None = None,
    source_name: str | None = None,
) -> list[dict[str, Any]]:
    query_params: dict[str, Any] = {}
    if source_name:
        query_params["source_name"] = source_name
    if date_from:
        query_params["date_from"] = date_from
    if date_to:
        query_params["date_to"] = date_to

    src_filter = _source_name_filter(source_name).replace("sourceName", "sys.sourceName")

    result = _get_con().sql(
        f"""
            SELECT sys.startDate AS date, sys.sourceName AS source,
            sys.value AS systolic, dia.value AS diastolic, hr.value AS heart_rate
            FROM records sys
            JOIN records dia
                ON sys.startDate = dia.startDate
                AND {normalized_source("sys.sourceName")} = {normalized_source("dia.sourceName")}
                AND dia.type = 'HKQuantityTypeIdentifierBloodPressureDiastolic'
            LEFT JOIN records hr
                ON hr.startDate = sys.startDate
                AND {normalized_source("sys.sourceName")} = {normalized_source("hr.sourceName")}
                AND hr.type = 'HKQuantityTypeIdentifierHeartRate'
            WHERE sys.type = 'HKQuantityTypeIdentifierBloodPressureSystolic'
            {src_filter}
            {"AND sys.startDate >= $date_from" if date_from else ""}
            {"AND sys.startDate <= $date_to" if date_to else ""}
            ORDER BY sys.startDate DESC
        """,
        params=query_params,
    )
    rows = client.format_response(result)
    for row in rows:
        # The LEFT JOIN leaves unmatched heart_rate as NaN (via pandas), not
        # None/NULL — normalize it so callers get valid JSON.
        if isinstance(row.get("heart_rate"), float) and math.isnan(row["heart_rate"]):
            row["heart_rate"] = None
    return rows


@ttl_cache(
    maxsize=64,
    ttl_seconds=settings.DUCKDB_QUERY_CACHE_TTL_SECONDS,
    enabled=_cache_enabled,
)
def search_values_from_duckdb(
    record_type: RecordType | WorkoutType | str | None,
    value: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    table = get_table(record_type)
    join_clause = join_string(table)

    params: dict[str, Any] = {"value": value}
    if record_type:
        params["record_type"] = sql_type(record_type)
    if date_from:
        params["date_from"] = sql_timestamp(date_from, "date_from")
    if date_to:
        params["date_to"] = sql_timestamp(date_to, "date_to")
    result = _get_con().sql(
        f"""
        SELECT * FROM {table} {join_clause} WHERE textValue = $value
        {f"AND {table}.type = $record_type" if record_type else ""}
        {"AND startDate >= $date_from" if date_from else ""}
        {"AND startDate <= $date_to" if date_to else ""}
        ORDER BY startDate DESC
        """,
        params=params,
    )
    return client.format_response(result)


logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, filename="duckdb.log", format="%(message)s")
    logger.info("Starting logging for duckdb queries")

    logger.info("-----------------")
    logger.info(f"records for get_health_summary_from_duckdb: {get_health_summary_from_duckdb()}")
    logger.info("-----------------")
    logger.info(
        f"records for get_statistics_by_type_duckdb:"
        f" {get_statistics_by_type_from_duckdb('HKWorkoutActivityTypeRunning')}",
    )
    logger.info("-----------------")
    logger.info(
        f"records for get_trend_data_duckdb: {
            get_trend_data_from_duckdb(
                'HKWorkoutActivityTypeRunning',
                date_from='2016-01-01T00:00:00+00:00',
                date_to='2016-12-31T23:59:59+00:00',
            )
        }",
    )
    logger.info("-----------------")
    pars = HealthRecordSearchParams(
        limit=20,
        record_type="HKWorkoutActivityTypeRunning",
        date_from="2016-01-01T00:00:00+00:00",
        date_to="2016-12-31T23:59:59+00:00",
    )
    logger.info(
        f"records for search_health_records_from_duckdb: {search_health_records_from_duckdb(pars)}",
    )
    logger.info("-----------------")
    logger.info("Finished logging")


if __name__ == "__main__":
    main()
