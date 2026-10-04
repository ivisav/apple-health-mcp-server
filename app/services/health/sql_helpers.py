import math
import re
from datetime import date, datetime
from typing import Any

from app.schemas.record import HealthRecordSearchParams

join_query: str = "INNER JOIN stats ON workouts.startDate = stats.startDate"

# Tool arguments come from an LLM and may carry injected text. Every value that is
# spliced into SQL below goes through one of these validators first, so it can never
# change the query's shape (these helpers are shared by DuckDB, Parquet and ClickHouse,
# which bind parameters differently).
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def sql_type(value: Any) -> str:
    """A record/workout type identifier such as ``HKQuantityTypeIdentifierHeartRate``."""
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise ValueError(
            "record_type must be an identifier like HKQuantityTypeIdentifierHeartRate, "
            f"got {value!r}"
        )
    return value


def sql_timestamp(value: Any, field: str) -> str:
    """An ISO date or timestamp, returned in normalized ISO form."""
    try:
        if isinstance(value, str) and len(value) == 10:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"{field} must be an ISO date or timestamp (YYYY-MM-DD[THH:MM:SS]), got {value!r}"
        ) from e


def sql_number(value: Any, field: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be a number, got {value!r}") from e
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    return str(number)


def sql_string(value: str) -> str:
    """A quoted SQL string literal (single quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


def join_string(table: str) -> str:
    if table == "workouts":
        return join_query
    return ""


def value_aggregates(table: str) -> list[str]:
    if table in ["workouts", "stats"]:
        return ["duration", "sum"]
    return ["value"]


def normalized_source(expr: str) -> str:
    """
    Wrap a SQL expression (a column like ``sourceName`` or a quoted literal) so two
    device names compare equal regardless of Unicode punctuation: fold curly
    apostrophes (U+2018/U+2019) to ``'``, no-break / narrow-no-break spaces
    (U+00A0/U+202F) to a normal space, then trim and lowercase.

    Apple stores e.g. ``Igor’s Apple Watch`` (curly apostrophe + NBSP), so a
    plain ``sourceName = 'Igor''s Apple Watch'`` never matches. Applied to BOTH
    sides of the comparison.
    """
    return (
        f"lower(trim(translate({expr}, "
        f"chr(8217) || chr(8216) || chr(160) || chr(8239), "
        f"chr(39) || chr(39) || chr(32) || chr(32))))"
    )


def get_table(record_type: str | list[str] | Any) -> str:
    types = record_type if isinstance(record_type, list) else [record_type]
    is_workout = [bool(t) and t.startswith("HKWorkout") for t in types]
    if any(is_workout) and not all(is_workout):
        raise ValueError(
            "record_type mixes HKWorkoutActivityType* with other types — these live in "
            "separate tables (workouts vs records) and can't be queried together. "
            "Split into separate calls per table.",
        )
    return "workouts" if any(is_workout) else "records"


def type_filter(table: str, record_type: str | list[str]) -> str:
    if isinstance(record_type, list):
        quoted = ", ".join(f"'{sql_type(t)}'" for t in record_type)
        return f"{table}.type IN ({quoted})"
    return f"{table}.type = '{sql_type(record_type)}'"


def get_value_type(table: str | None) -> str:
    match table:
        case "records":
            return "value"
        case "workouts" | "stats":
            return "sum"
        case _:
            return "value"


def build_date(date_from: str | None, date_to: str | None, table: str) -> str | None:
    start = sql_timestamp(date_from, "date_from") if date_from else None
    end = sql_timestamp(date_to, "date_to") if date_to else None
    if start and end:
        return f"{table}.startDate >= '{start}' and {table}.startDate <= '{end}'"
    if start:
        return f"{table}.startDate >= '{start}'"
    if end:
        return f"{table}.startDate <= '{end}'"
    return None


def build_value_range(
    valuemin: str | None,
    valuemax: str | None,
    value_type: str | None,
) -> str | None:
    # value_type: str = get_value_type(table)

    low = sql_number(valuemin, "value_min") if valuemin else None
    high = sql_number(valuemax, "value_max") if valuemax else None
    if low and high:
        return f"{value_type} >= {low} and {value_type} <= {high}"
    if low:
        return f"{value_type} >= {low}"
    if high:
        return f"{value_type} <= {high}"
    return None


def fill_query(params: HealthRecordSearchParams) -> str:
    conditions: list[str] = []
    table = get_table(params.record_type)

    if table == "workouts":
        query: str = f" workouts {join_query} WHERE 1=1"
    else:
        query: str = " records WHERE 1=1"
    value_type = get_value_type(table)

    if params.record_type:
        conditions.append(f" {type_filter(table, params.record_type)}")
    if params.source_name:
        source = normalized_source(sql_string(params.source_name))
        conditions.append(f" {normalized_source('sourceName')} = {source}")
    if params.date_from or params.date_to:
        conditions.append(build_date(params.date_from, params.date_to, table))
    if params.value_min or params.value_max:
        conditions.append(build_value_range(params.value_min, params.value_max, value_type))
    if params.min_workout_duration or params.max_workout_duration:
        conditions.append(
            build_value_range(params.min_workout_duration, params.max_workout_duration, "duration")
        )

    conditions = [condition for condition in conditions if condition is not None]

    if conditions:
        query += " AND " + " AND ".join(conditions)

    if isinstance(params.record_type, list):
        query += (
            f" QUALIFY ROW_NUMBER() OVER ("
            f"PARTITION BY {table}.type ORDER BY {table}.startDate DESC"
            f") <= {int(params.limit)} ORDER BY {table}.startDate DESC"
        )
    else:
        query += f" ORDER BY {table}.startDate DESC LIMIT {int(params.limit)}"
    return query
