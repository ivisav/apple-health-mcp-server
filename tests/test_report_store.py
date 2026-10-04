from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from app.services.health import report_store
from app.services.health.report_store import (
    add_report_notes,
    get_report_history,
    get_report_notes,
    get_report_trend,
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


# --- read path -----------------------------------------------------------


def _seed_four_weeks() -> list[str]:
    ids = []
    data = [  # (start, end, generated, hrv, rhr, weight)
        ("2026-02-16", "2026-02-22", "2026-02-22T10:00:00", 48.0, 60.0, 92.0),
        ("2026-02-23", "2026-03-01", "2026-03-01T10:00:00", 50.0, 59.0, 91.5),
        ("2026-03-02", "2026-03-08", "2026-03-08T10:00:00", 47.0, 61.0, 91.0),
        ("2026-03-09", "2026-03-15", "2026-03-15T10:00:00", 52.0, 58.0, 90.0),
    ]
    for start, end, gen, hrv, rhr, weight in data:
        pid = _period(start, end, gen)
        upsert_report_metrics(
            pid,
            [
                {"key": "hrv_avg_ms", "value": hrv},
                {"key": "rhr_avg_bpm", "value": rhr},
                {"key": "weight_avg_kg", "value": weight},
            ],
            "weekly-health-report",
        )
        ids.append(pid)
    return ids


def test_history_on_fresh_store_is_empty_not_error(temp_reports_db: Path) -> None:
    fresh = temp_reports_db.parent / "nested" / "fresh.duckdb"
    report_store.store.path = fresh
    assert get_report_history() == {"periods": [], "metrics": {}, "notes": {}, "keys_in_use": {}}
    assert get_report_trend(["hrv_avg_ms"], "month")["buckets"] == []
    assert get_report_notes() == []
    assert fresh.exists()


def test_history_matrix_aggregates_and_direction() -> None:
    ids = _seed_four_weeks()

    h = get_report_history(metrics=["hrv_avg_ms", "rhr_avg_bpm"], last_n=3)

    assert [p["period_id"] for p in h["periods"]] == [ids[3], ids[2], ids[1]]
    hrv = h["metrics"]["hrv_avg_ms"]
    assert hrv["values"] == [52.0, 47.0, 50.0]
    assert hrv["unit"] == "ms"
    assert hrv["avg_last_n"] == pytest.approx(49.667, abs=1e-3)
    assert hrv["delta_vs_prev"] == 5.0
    assert hrv["direction"] == "higher_is_better"
    assert hrv["all_time_max"] == {"value": 52.0, "cp_end": "2026-03-15"}
    assert hrv["all_time_min"] == {
        "value": 47.0,
        "cp_end": "2026-03-08",
    }  # all time, not only last_n
    assert h["metrics"]["rhr_avg_bpm"]["direction"] == "lower_is_better"
    assert "weight_avg_kg" not in h["metrics"]
    assert "keys_in_use" not in h


def test_history_all_keys_text_values_and_notes() -> None:
    ids = _seed_four_weeks()
    upsert_report_metrics(
        ids[3], [{"key": "training_status", "value_text": "Productive"}], "weekly-health-report"
    )
    add_report_notes(ids[3], "finding", ["HRV up"], "weekly-health-report")
    add_report_notes(ids[3], "flag", ["low steps"], "weekly-health-report")
    add_report_notes(ids[2], "finding", ["older finding"], "weekly-health-report")

    h = get_report_history(last_n=2)

    assert h["metrics"]["training_status"]["values"] == ["Productive", None]
    assert h["metrics"]["training_status"]["avg_last_n"] is None
    assert h["notes"] == {"finding": ["HRV up"], "flag": ["low steps"]}
    assert h["keys_in_use"] == {
        "hrv_avg_ms": 4,
        "rhr_avg_bpm": 4,
        "training_status": 1,
        "weight_avg_kg": 4,
    }


def test_empty_period_is_ignored_by_history_and_previous() -> None:
    ids = _seed_four_weeks()
    _period("2026-03-16", "2026-03-18", "2026-03-18T10:00:00")  # crashed run

    h = get_report_history(last_n=1)
    assert h["periods"][0]["period_id"] == ids[3]


def test_history_rejects_bad_last_n() -> None:
    with pytest.raises(ValueError, match="last_n"):
        get_report_history(last_n=0)


def test_trend_week_and_month_day_weighted() -> None:
    _seed_four_weeks()
    long_pid = _period("2026-03-16", "2026-03-29", "2026-03-29T10:00:00")  # 14 days
    upsert_report_metrics(long_pid, [{"key": "hrv_avg_ms", "value": 60.0}], "weekly-health-report")

    week = get_report_trend(["hrv_avg_ms", "vo2max"], "week")
    assert week["buckets"] == ["2026-W08", "2026-W09", "2026-W10", "2026-W11", "2026-W13"]
    assert week["series"]["hrv_avg_ms"] == [48.0, 50.0, 47.0, 52.0, 60.0]
    assert week["series"]["vo2max"] == [None] * 5

    month = get_report_trend(["hrv_avg_ms"], "month")
    assert month["buckets"] == ["2026-02", "2026-03"]
    assert month["n_periods"] == [1, 4]
    assert month["days"] == [7.0, 35.0]
    # March: (50*7 + 47*7 + 52*7 + 60*14) / 35
    assert month["series"]["hrv_avg_ms"][1] == pytest.approx(
        (50 * 7 + 47 * 7 + 52 * 7 + 60 * 14) / 35, abs=1e-3
    )


def test_trend_date_filter_and_validation() -> None:
    _seed_four_weeks()
    t = get_report_trend(["hrv_avg_ms"], "week", date_from="2026-03-01", date_to="2026-03-08")
    assert t["buckets"] == ["2026-W09", "2026-W10"]
    with pytest.raises(ValueError, match="metrics"):
        get_report_trend([], "week")
    with pytest.raises(ValueError, match="granularity"):
        get_report_trend(["hrv_avg_ms"], "year")


def test_notes_filters_and_latest_per_subject() -> None:
    ids = _seed_four_weeks()
    src = "weekly-health-report-recommend"
    add_report_notes(
        ids[0],
        "validated_intervention",
        [{"text": "old verdict", "subject": "Example timing", "noted_at": "2026-02-20"}],
        src,
        replace=False,
    )
    add_report_notes(
        ids[3],
        "validated_intervention",
        [
            {"text": "new verdict", "subject": "example TIMING", "noted_at": "2026-03-14"},
            {"text": "other", "subject": "Other thing", "noted_at": "2026-03-10"},
        ],
        src,
        replace=False,
    )
    add_report_notes(ids[3], "recommendation", ["sleep earlier"], src)

    latest = get_report_notes(kinds=["validated_intervention"], latest_per_subject=True)
    assert [n["text"] for n in latest] == ["new verdict", "other"]

    by_subject = get_report_notes(subject="example timing")
    assert [n["text"] for n in by_subject] == ["new verdict", "old verdict"]
    assert by_subject[0]["cp_end"] == "2026-03-15"

    recent = get_report_notes(since="2026-03-12")
    assert {n["text"] for n in recent} == {"new verdict", "sleep earlier"}

    with pytest.raises(ValueError, match="kind"):
        get_report_notes(kinds=["gossip"])


def test_notes_keep_write_order_not_alphabetical() -> None:
    # Section 3 order matters (positives first, then concerns by severity);
    # notes written in one call share created_at, so order must not fall back to text.
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    _metric(pid, "ctl", 40)
    add_report_notes(pid, "finding", ["Zeta went well", "Alpha concern"], "weekly-health-report")

    assert get_report_history(last_n=1)["notes"]["finding"] == ["Zeta went well", "Alpha concern"]
    assert [n["text"] for n in get_report_notes(kinds=["finding"])] == ["Zeta went well", "Alpha concern"]


def test_history_rejects_empty_metrics_list() -> None:
    with pytest.raises(ValueError, match="metrics"):
        get_report_history(metrics=[])


def test_latest_per_subject_tie_break_prefers_last_written() -> None:
    pid = _period("2026-03-09", "2026-03-15", "2026-03-15T10:00:00")
    add_report_notes(
        pid, "validated_intervention",
        [{"text": "first", "subject": "S", "noted_at": "2026-03-14"},
         {"text": "second", "subject": "S", "noted_at": "2026-03-14"}],
        "x", replace=False,
    )
    _metric(pid, "ctl", 40)
    latest = get_report_notes(kinds=["validated_intervention"], latest_per_subject=True)
    assert [n["text"] for n in latest] == ["second"]
