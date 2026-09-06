import pytest

from app.schemas.record import HealthRecordSearchParams
from app.services.health.sql_helpers import (
    fill_query,
    get_table,
    normalized_source,
    type_filter,
)


def test_get_table_single_type() -> None:
    assert get_table("HKQuantityTypeIdentifierStepCount") == "records"
    assert get_table("HKWorkoutActivityTypeRunning") == "workouts"


def test_get_table_none() -> None:
    assert get_table(None) == "records"


def test_get_table_homogeneous_list() -> None:
    assert get_table(["HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierHeartRate"]) == "records"
    assert get_table(["HKWorkoutActivityTypeRunning", "HKWorkoutActivityTypeWalking"]) == "workouts"


def test_get_table_mixed_list_raises() -> None:
    with pytest.raises(ValueError, match="mixes"):
        get_table(["HKWorkoutActivityTypeRunning", "HKQuantityTypeIdentifierStepCount"])


def test_type_filter_single() -> None:
    assert type_filter("records", "HKQuantityTypeIdentifierStepCount") == (
        "records.type = 'HKQuantityTypeIdentifierStepCount'"
    )


def test_type_filter_list() -> None:
    assert type_filter("records", ["HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierHeartRate"]) == (
        "records.type IN ('HKQuantityTypeIdentifierStepCount', 'HKQuantityTypeIdentifierHeartRate')"
    )


def test_normalized_source_folds_both_sides() -> None:
    snippet = normalized_source("sourceName")
    # references the real column, not the snake_case name that does not exist
    assert "sourceName" in snippet
    assert "source_name" not in snippet
    # folds curly apostrophe + NBSP, then lowercases / trims
    assert "chr(8217)" in snippet and "chr(160)" in snippet
    assert snippet.startswith("lower(trim(translate(")


def test_fill_query_source_name_uses_camel_case_column() -> None:
    query = fill_query(
        HealthRecordSearchParams(
            record_type="HKQuantityTypeIdentifierStepCount",
            source_name="Igor's Apple Watch",
            limit=5,
        ),
    )
    # regression for the "source_name not found" binder error
    assert " source_name = " not in query
    assert "sourceName" in query
    # normalization is applied to both the column and the literal
    assert query.count("translate(") >= 2


def test_fill_query_escapes_source_name_quote() -> None:
    query = fill_query(
        HealthRecordSearchParams(
            record_type="HKQuantityTypeIdentifierStepCount",
            source_name="x' OR '1'='1",
            limit=5,
        ),
    )
    assert "'x'' OR ''1''=''1'" in query
