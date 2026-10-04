from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from app.services.health import report_store
from app.services.health.report_store import (
    add_report_notes,
    start_report_period,
    upsert_report_metrics,
)


@pytest.fixture(autouse=True)
def temp_reports_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "health_reports.duckdb"
    monkeypatch.setattr(report_store.store, "path", db_path)
    yield db_path


def _period(start: str, end: str, generated: str) -> str:
    return start_report_period(start, end, generated)["period_id"]


def _metric(period_id: str, key: str, value: float, skill: str = "weekly-health-report") -> None:
    upsert_report_metrics(period_id, [{"key": key, "value": value}], skill)


# --- start_report_period -------------------------------------------------

def test_start_report_period_creates_row_with_iso_week() -> None:
    p = start_report_period("2026-03-09", "2026-03-15", "2026-03-15T18:30:00")
    assert p["created"] is True
    assert p["cp_start"] == "2026-03-09"
    assert p["cp_end"] == "2026-03-15"
    assert p["days"] == 7.0
    assert (p["iso_year"], p["iso_week"]) == (2026, 11)
    assert p["previous_periods"] == []


def test_start_report_period_strips_timezone_and_is_idempotent() -> None:
    a = start_report_period("2026-03-09", "2026-03-15", "2026-03-15T18:30:00+01:00")
    b = start_report_period("2026-03-09", "2026-03-15", "2026-03-15T18:30:00")
    assert a["period_id"] == b["period_id"]
    assert b["created"] is False
    assert b["report_generated"] == "2026-03-15T18:30:00"


def test_start_report_period_explicit_days() -> None:
    p = start_report_period("2026-03-09", "2026-03-15", "2026-03-15T18:30:00", days=6.5)
    assert p["days"] == 6.5


@pytest.mark.parametrize(
    ("start", "end", "generated", "match"),
    [
        ("2026-03-15", "2026-03-09", "2026-03-15T00:00:00", "cp_end"),
        ("15/03/2026", "2026-03-15", "2026-03-15T00:00:00", "cp_start"),
        ("2026-03-09", "2026-03-15", "yesterday", "report_generated"),
    ],
)
def test_start_report_period_rejects_bad_input(start: str, end: str, generated: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        start_report_period(start, end, generated)


def test_previous_periods_newest_first_and_skip_empty() -> None:
    p1 = _period("2026-02-23", "2026-03-01", "2026-03-01T10:00:00")
    p2 = _period("2026-03-02", "2026-03-08", "2026-03-08T10:00:00")
    _metric(p1, "hrv_avg_ms", 50)
    _metric(p2, "hrv_avg_ms", 51)
    _period("2026-03-09", "2026-03-12", "2026-03-12T10:00:00")  # crashed run: no metrics

    cur = start_report_period("2026-03-12", "2026-03-15", "2026-03-15T10:00:00")

    assert [p["period_id"] for p in cur["previous_periods"]] == [p2, p1]


# --- upsert_report_metrics -----------------------------------------------

def test_upsert_overwrites_same_key_and_defaults_unit() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    upsert_report_metrics(pid, [{"key": "hrv_avg_ms", "value": 50.0}], "weekly-health-report")
    res = upsert_report_metrics(pid, [{"key": "hrv_avg_ms", "value": 52.5, "note": "fixed"}], "weekly-health-report")

    assert res == {"period_id": pid, "upserted": 1, "new_keys": []}
    con = duckdb.connect(str(report_store.store.path), read_only=True)
    try:
        rows = con.execute("SELECT key, value, unit, note FROM report_metrics").fetchall()
    finally:
        con.close()
    assert rows == [("hrv_avg_ms", 52.5, "ms", "fixed")]


def test_upsert_text_metric_and_skills_coexist() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    upsert_report_metrics(pid, [{"key": "training_status", "value_text": "Productive"}], "weekly-health-report")
    upsert_report_metrics(pid, [{"key": "protein_g_day", "value": 120}], "weekly-health-report-detail")

    con = duckdb.connect(str(report_store.store.path), read_only=True)
    try:
        rows = con.execute("SELECT key, value_text, source_skill FROM report_metrics ORDER BY key").fetchall()
    finally:
        con.close()
    assert rows == [
        ("protein_g_day", None, "weekly-health-report-detail"),
        ("training_status", "Productive", "weekly-health-report"),
    ]


def test_upsert_reports_new_non_core_keys_once() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    first = upsert_report_metrics(pid, [{"key": "ride_quality_score", "value": 4}, {"key": "ctl", "value": 40}], "x")
    second = upsert_report_metrics(pid, [{"key": "ride_quality_score", "value": 5}], "x")
    assert first["new_keys"] == ["ride_quality_score"]
    assert second["new_keys"] == []


def test_upsert_rejects_duplicate_keys_and_nan() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    with pytest.raises(ValueError, match="duplicate"):
        upsert_report_metrics(pid, [{"key": "ctl", "value": 1}, {"key": "ctl", "value": 2}], "x")
    with pytest.raises(ValidationError, match="finite"):
        upsert_report_metrics(pid, [{"key": "ctl", "value": float("nan")}], "x")


@pytest.mark.parametrize(
    "metric",
    [{"key": "HRV avg", "value": 1}, {"key": "ctl"}, {"key": "", "value": 1}],
)
def test_upsert_rejects_bad_metric(metric: dict) -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    with pytest.raises(ValidationError):
        upsert_report_metrics(pid, [metric], "x")


def test_upsert_rejects_unknown_or_malformed_period_and_empty_input() -> None:
    with pytest.raises(ValueError, match="No report period"):
        upsert_report_metrics("00000000-0000-0000-0000-000000000000", [{"key": "ctl", "value": 1}], "x")
    with pytest.raises(ValueError, match="UUID"):
        upsert_report_metrics("not-a-uuid", [{"key": "ctl", "value": 1}], "x")
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    with pytest.raises(ValueError, match="empty"):
        upsert_report_metrics(pid, [], "x")
    with pytest.raises(ValueError, match="source_skill"):
        upsert_report_metrics(pid, [{"key": "ctl", "value": 1}], " ")


def test_upsert_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    real_default_unit = report_store._default_unit

    def boom(key: str) -> str | None:
        if key == "tsb":
            raise RuntimeError("simulated failure mid-write")
        return real_default_unit(key)

    monkeypatch.setattr(report_store, "_default_unit", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        upsert_report_metrics(pid, [{"key": "ctl", "value": 40}, {"key": "tsb", "value": -5}], "x")

    con = duckdb.connect(str(report_store.store.path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM report_metrics").fetchone() == (0,)
    finally:
        con.close()


# --- add_report_notes ----------------------------------------------------

def test_add_report_notes_replace_scoped_to_skill_and_kind() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    add_report_notes(pid, "finding", ["old finding"], "weekly-health-report")
    add_report_notes(pid, "finding", ["detail finding"], "weekly-health-report-detail")
    add_report_notes(pid, "flag", ["a flag"], "weekly-health-report")

    res = add_report_notes(pid, "finding", ["new finding 1", {"text": "new finding 2"}], "weekly-health-report")

    assert res == {"period_id": pid, "kind": "finding", "added": 2, "replaced": 1}
    con = duckdb.connect(str(report_store.store.path), read_only=True)
    try:
        rows = con.execute("SELECT kind, text FROM report_notes ORDER BY kind, text").fetchall()
    finally:
        con.close()
    assert rows == [
        ("finding", "detail finding"),
        ("finding", "new finding 1"),
        ("finding", "new finding 2"),
        ("flag", "a flag"),
    ]


def test_add_report_notes_append_with_subject_and_noted_at() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    add_report_notes(
        pid, "validated_intervention",
        [{"text": "supported — source A", "subject": "Example timing", "noted_at": "2026-03-01"}],
        "weekly-health-report-recommend", replace=False,
    )
    con = duckdb.connect(str(report_store.store.path), read_only=True)
    try:
        row = con.execute("SELECT subject, CAST(created_at AS DATE) FROM report_notes").fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == "Example timing"
    assert str(row[1]) == "2026-03-01"


def test_add_report_notes_rejects_unknown_kind() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    with pytest.raises(ValueError, match="kind"):
        add_report_notes(pid, "gossip", ["x"], "x")


# --- durability / fresh build --------------------------------------------

def test_writes_are_durable_without_wal(temp_reports_db: Path, tmp_path: Path) -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    _metric(pid, "ctl", 40)
    copy = tmp_path / "copy.duckdb"
    copy.write_bytes(temp_reports_db.read_bytes())
    con = duckdb.connect(str(copy), read_only=True)
    try:
        assert con.execute("SELECT key, value FROM report_metrics").fetchall() == [("ctl", 40.0)]
    finally:
        con.close()
