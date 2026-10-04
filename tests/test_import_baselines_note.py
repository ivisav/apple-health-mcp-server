from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.health import report_store
from scripts import import_baselines_note as imp

NOTE = """# Weekly Health Baselines

## Period of Mar 9 – Mar 15, 2026 (reported 2026-03-15)
**Report generated**: 2026-03-15T18:30:00+01:00 — this becomes CP_start for the next run
**Period covered**: 2026-03-09 – 2026-03-15 (7 days)

| Metric | Value | Notes |
|--------|-------|-------|
| Sleep Score | 80/100 Good | composite |
| Sleep Consistency | Fair | 35min stddev sleep start |
| Recovery Status | 7/10 Good | composite HRV+RHR+TSB+HRR |
| HRV avg | 50.0 ms | baseline 48.5 ms |
| RHR avg | 58.0 bpm | |
| Body weight | 90.00 kg | range 89.5–90.4 |
| Body fat % | 20.0% | LBM 72.0 kg |
| Steps avg/day | 8,000 | Watch only |
| VO₂ Max | 40.0 mL/min·kg | trend: → |
| Protein/day | — | run weekly-health-report-detail |
| Mystery metric | 3 | |
| Deep sleep avg (raw) | 60 min | 7 nights tracked — raw, no correction |
| Blood pressure | 120/80 avg | 3 readings this period — Normal |
| Alcohol | none | |

**Key findings**:
- HRV up vs baseline
- Weight steady
**Flags**: low steps, short sleep
**Training load context**: CTL 40.0 · ATL 45.5 · TSB −5.5

**Validated interventions** (cache):
| Intervention | Last validated | Verdict |
|---|---|---|
| Example supplement timing | 2026-03-01 | supported — source A |

## Detail supplement for period 2026-03-09 – 2026-03-15
| Protein/day | 120 g | |

## Week of Mar 2–8, 2026 (reported 2026-03-09)
| Metric | Value | Notes |
|---|---|---|
| Deep sleep avg | 82.5 min ×1.65 | corrected |
| HRV avg | 49.0 ms | |
**Key findings**: First tracked week
**Flags**: none
**Training load context**: CTL 38.0 · ATL 40.0 · TSB −2.0
"""


@pytest.fixture(autouse=True)
def temp_reports_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "health_reports.duckdb"
    monkeypatch.setattr(report_store.store, "path", db_path)
    yield db_path


def _values(period: imp.ParsedPeriod) -> dict[str, float | str | None]:
    return {
        k: (m.value if m.value is not None else m.value_text) for k, m in period.metrics.items()
    }


def test_parses_new_format_block() -> None:
    parsed = imp.parse_note(NOTE)
    new = parsed.periods[0]
    assert (new.cp_start, new.cp_end, new.days) == (date(2026, 3, 9), date(2026, 3, 15), 7.0)
    assert new.report_generated == datetime(2026, 3, 15, 18, 30)
    v = _values(new)
    assert v["sleep_score"] == 80.0
    assert v["sleep_consistency"] == "Fair"
    assert v["sleep_consistency_stddev_min"] == 35.0
    assert v["recovery_status"] == 7.0
    assert v["hrv_avg_ms"] == 50.0
    assert v["hrv_baseline_ms"] == 48.5
    assert v["weight_avg_kg"] == 90.0
    assert v["body_fat_pct"] == 20.0
    assert v["lbm_kg"] == 72.0
    assert v["steps_avg_day"] == 8000.0
    assert v["vo2max"] == 40.0
    assert v["deep_sleep_raw_min"] == 60.0
    assert (v["bp_systolic_mmhg"], v["bp_diastolic_mmhg"], v["bp_category"]) == (
        120.0,
        80.0,
        "Normal",
    )
    assert v["alcohol_drinks"] == 0.0
    assert v["protein_g_day"] == 120.0  # merged from the supplement block
    assert (v["ctl"], v["atl"], v["tsb"]) == (40.0, 45.5, -5.5)
    assert new.findings == ["HRV up vs baseline", "Weight steady"]
    assert new.flags == ["low steps", "short sleep"]
    assert [(n.subject, n.noted_at) for n in new.interventions] == [
        ("Example supplement timing", "2026-03-01")
    ]


def test_parses_legacy_block_and_normalizes_deep_sleep() -> None:
    legacy = imp.parse_note(NOTE).periods[1]
    assert (legacy.cp_start, legacy.cp_end, legacy.days) == (
        date(2026, 3, 2),
        date(2026, 3, 8),
        7.0,
    )
    assert legacy.report_generated == datetime(2026, 3, 9, 0, 0)
    v = _values(legacy)
    assert v["deep_sleep_raw_min"] == pytest.approx(50.0)
    assert legacy.findings == ["First tracked week"]
    assert legacy.flags == []


def test_parses_unicode_minus_and_thousands() -> None:
    assert imp.first_number("TSB −5.5") == -5.5
    assert imp.first_number("~1,453 kcal") == 1453.0
    assert imp.first_number("—") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-28 – 2026-10-04 (6.0 days)", (date(2026, 9, 28), date(2026, 10, 4))),
        ("Sep 28 – Oct 4, 2026", (date(2026, 9, 28), date(2026, 10, 4))),
        ("Aug 24–30, 2026", (date(2026, 8, 24), date(2026, 8, 30))),
        ("Dec 29 – Jan 4, 2027", (date(2026, 12, 29), date(2027, 1, 4))),
        ("Aug 24 – 30", (date(2026, 8, 24), date(2026, 8, 30))),
    ],
)
def test_parse_date_range(text: str, expected: tuple[date, date]) -> None:
    assert imp.parse_date_range(text, fallback_year=2026) == expected


def test_warnings_for_unmapped_rows() -> None:
    parsed = imp.parse_note(NOTE)
    assert any("Mystery metric" in w for w in parsed.warnings)


def test_dry_run_writes_nothing(
    temp_reports_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "note.md"
    src.write_text(NOTE)
    assert imp.main([str(src), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "hrv_avg_ms = 50.0" in out
    assert not temp_reports_db.exists()


def test_import_is_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "note.md"
    src.write_text(NOTE)
    assert imp.main([str(src)]) == 0
    assert imp.main([str(src)]) == 0

    h = report_store.get_report_history(last_n=10)
    assert len(h["periods"]) == 2
    assert h["metrics"]["hrv_avg_ms"]["values"] == [50.0, 49.0]
    assert h["notes"] == {
        "finding": ["HRV up vs baseline", "Weight steady"],
        "flag": ["low steps", "short sleep"],
    }
    cache = report_store.get_report_notes(kinds=["validated_intervention"], latest_per_subject=True)
    assert [n["subject"] for n in cache] == ["Example supplement timing"]


def test_no_blocks_returns_error_code(tmp_path: Path) -> None:
    src = tmp_path / "empty.md"
    src.write_text("# nothing here\n")
    assert imp.main([str(src)]) == 1
