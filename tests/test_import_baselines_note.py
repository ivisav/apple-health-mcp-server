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


VARIANTS = """# Weekly Health Baselines

## Period of 2026-04-06 – 2026-04-12 (reported 2026-04-12)
  **Report generated:** 2026-04-12T09:00:00 — this becomes CP_start for the next run
  **Period covered:** 2026-04-06 – 2026-04-12 (7 days)

| Metric | Value | Notes |
|---|---|---|
| HRV avg | 51.0 ms | |
**Key findings:**
- Something good
**Flags:**
- flag one
- flag two
**Training load context**: CTL 41.0 · ATL 38.0 · TSB +3.0

## Some unrelated heading
| HRV avg | 99.0 ms | |
**Flags**: should not leak

## Period of 2026-03-30 – 2026-04-05 (reported 2026-04-05)
| HRV avg | 50.0 ms | |
**Flags**: a; b · c
"""


def test_colon_inside_bold_and_indented_meta_lines() -> None:
    p = imp.parse_note(VARIANTS).periods[0]
    assert p.report_generated == datetime(2026, 4, 12, 9, 0)
    assert p.days == 7.0
    assert p.findings == ["Something good"]


def test_flags_as_bullets_and_other_separators() -> None:
    newer, older = imp.parse_note(VARIANTS).periods
    assert newer.flags == ["flag one", "flag two"]
    assert older.flags == ["a", "b", "c"]


def test_positive_tsb_with_plus_sign() -> None:
    p = imp.parse_note(VARIANTS).periods[0]
    assert _values(p)["tsb"] == 3.0


def test_unrecognised_heading_does_not_leak_into_previous_period() -> None:
    parsed = imp.parse_note(VARIANTS)
    newer = parsed.periods[0]
    assert _values(newer)["hrv_avg_ms"] == 51.0
    assert "should not leak" not in newer.flags
    assert any("Some unrelated heading" in w for w in parsed.warnings)


SAME_DAY = """## Period of 2026-03-09 – 2026-03-15 (reported 2026-03-16)
| HRV avg | 50.0 ms | |
**Key findings**: week A

## Period of 2026-03-16 – 2026-03-16 (reported 2026-03-16)
| HRV avg | 60.0 ms | |
**Key findings**: week B
"""


def test_same_report_generated_keeps_both_periods_with_warning() -> None:
    parsed = imp.parse_note(SAME_DAY)
    assert len({p.report_generated for p in parsed.periods}) == 2
    assert any("duplicate" in w for w in parsed.warnings)

    imp.write_periods(parsed)
    h = report_store.get_report_history(last_n=5)
    assert sorted(p["cp_start"] for p in h["periods"]) == ["2026-03-09", "2026-03-16"]
    assert sorted(v for v in h["metrics"]["hrv_avg_ms"]["values"]) == [50.0, 60.0]


def test_write_refuses_period_that_conflicts_with_store() -> None:
    report_store.start_report_period("2026-03-01", "2026-03-08", "2026-03-16T00:00:00")
    parsed = imp.parse_note(SAME_DAY.split("\n\n")[0] + "\n")
    result = imp.write_periods(parsed)
    assert result["periods"] == 0
    assert len(result["skipped"]) == 1


def test_imported_notes_are_dated_to_their_period(tmp_path: Path) -> None:
    src = tmp_path / "note.md"
    src.write_text(NOTE)
    assert imp.main([str(src)]) == 0
    texts = {n["text"] for n in report_store.get_report_notes(since="2026-03-12")}
    assert "First tracked week" not in texts  # legacy period reported 2026-03-09
    assert "HRV up vs baseline" in texts
    finding = report_store.get_report_notes(kinds=["finding"], subject=None)
    assert {n["created_at"][:10] for n in finding} == {"2026-03-15", "2026-03-09"}


SUBHEADINGS = """## Period of 2026-04-06 – 2026-04-12 (reported 2026-04-12)
| HRV avg | 51.0 ms | baseline: 49.5 ms |
| Body fat % | 21.0% | LBM: 70.0 kg |
| Water avg/day | 2.1 L | |
### Training
**Training load context**: CTL 41.0 · ATL 38.0 · TSB 3.0
**Flags**: low steps; none
"""


def test_level3_heading_stays_inside_block_and_colon_variants() -> None:
    parsed = imp.parse_note(SUBHEADINGS)
    v = _values(parsed.periods[0])
    assert v["ctl"] == 41.0
    assert v["hrv_baseline_ms"] == 49.5
    assert v["lbm_kg"] == 70.0
    assert v["water_avg_ml_day"] == 2100.0
    assert parsed.periods[0].flags == ["low steps"]
    assert not any("Training" in w for w in parsed.warnings)


def test_in_block_report_generated_wins_over_unparseable_header_time() -> None:
    text = """## Period of Mar 9 – 15, 2026 (reported Mar 15, 2026 18:30)
**Report generated**: 2026-03-15T18:30:00
| HRV avg | 50.0 ms | |
"""
    parsed = imp.parse_note(text)
    assert [p.report_generated for p in parsed.periods] == [datetime(2026, 3, 15, 18, 30)]
    header_only = imp.parse_note(text.replace("**Report generated**: 2026-03-15T18:30:00\n", ""))
    assert [p.report_generated for p in header_only.periods] == [datetime(2026, 3, 15, 0, 0)]


# Synthetic note in the flat, single-paragraph form Claude's Apple Notes writes
# produce: every block, table row and label run together on one line.
FLAT = (
    "Weekly Health Baselines Historical reports. Newest entries at top. "
    "## Period of Mar 9 – 15, 2026 (reported 2026-03-15, detail supplement added 2026-03-16) "
    "**Report generated**: 2026-03-15T18:30:00+01:00 — this becomes CP_start for the next run "
    "**Period covered**: 2026-03-09 – 2026-03-15 (6.5 days) "
    "| Metric | Value | Notes | |--------|-------|-------| "
    "| Sleep Score | 80/100 Good | composite | | HRV avg | 50.0 ms | baseline 48.5 ms | "
    "| RHR avg | 58.0 bpm | | | Avg speed | 25.0 km/h | road | | Stand time avg | 70 min/day | | "
    "**Key findings**: 1. HRV up vs baseline, sleep steady. 2. Weight down 0.5 kg. "
    "**Flags**: Low steps (monitor) · Hydration thin, event day unlogged (trend) "
    "**Training load context**: CTL 40.0 · ATL 45.5 · TSB -5.5 (as of 2026-03-14) "
    "**Detail supplement** (detail v1.1, 2026-03-16): Fiber low on 3/6 days. "
    "**Validated interventions** (cache): | Intervention | Last validated | Verdict | |---|---|---| "
    "| Example timing | 2026-03-01 | supported — source A | "
    "## Period of Mar 2 – Mar 8, 2026 (reported Mar 8, 2026) "
    "Report generated: 2026-03-08T20:00:00+01:00 — this becomes CP_start "
    "Period covered: 2026-03-02 – 2026-03-08 (7 days; one night missing) "
    "Metric | Value | Notes Cycling sessions | 2, both indoor | ramp test "
    "Training load | CTL 38.0 · TSB -2.0 | expected fatigue Weekly km | 60.0km | indoor "
    "## Week of Feb 23–Mar 1, 2026 (reported Mar 1, corrected Mar 3) "
    "Metric | Value | Notes HRV avg | 49.0 ms | ok "
    "## FTP & Zone Update — confirmed Mar 1, 2026 - Ramp test done"
)


def test_flat_note_new_format_block() -> None:
    parsed = imp.parse_note(FLAT)
    assert len(parsed.periods) == 3
    p = parsed.periods[0]
    assert (p.cp_start, p.cp_end, p.days) == (date(2026, 3, 9), date(2026, 3, 15), 6.5)
    assert p.report_generated == datetime(2026, 3, 15, 18, 30)
    v = _values(p)
    assert (v["sleep_score"], v["hrv_avg_ms"], v["hrv_baseline_ms"], v["rhr_avg_bpm"]) == (80.0, 50.0, 48.5, 58.0)
    assert (v["avg_speed_kmh"], v["stand_min_day"]) == (25.0, 70.0)
    assert (v["ctl"], v["atl"], v["tsb"]) == (40.0, 45.5, -5.5)
    assert p.findings == ["HRV up vs baseline, sleep steady.", "Weight down 0.5 kg."]
    assert p.flags == ["Low steps (monitor)", "Hydration thin, event day unlogged (trend)"]
    assert [n.subject for n in p.interventions] == ["Example timing"]


def test_flat_note_bare_tables_and_legacy_headers() -> None:
    parsed = imp.parse_note(FLAT)
    mid, old = parsed.periods[1], parsed.periods[2]
    assert mid.report_generated == datetime(2026, 3, 8, 20, 0)
    assert mid.days == 7.0
    v = _values(mid)
    assert (v["cycling_sessions"], v["ctl"], v["tsb"], v["total_km"]) == (2.0, 38.0, -2.0, 60.0)
    assert (old.cp_start, old.cp_end) == (date(2026, 2, 23), date(2026, 3, 1))
    assert old.report_generated == datetime(2026, 3, 1, 0, 0)
    assert _values(old)["hrv_avg_ms"] == 49.0
    assert any("FTP & Zone Update" in w for w in parsed.warnings)


def test_reads_notes_html_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "note.html"
    src.write_text("<div>" + FLAT.replace("&", "&amp;") + "</div>")
    assert imp.main([str(src), "--dry-run"]) == 0
    assert "hrv_avg_ms = 50.0" in capsys.readouterr().out


def test_number_regexes_need_a_digit() -> None:
    assert [m.key for m in imp._row_metrics("hrv avg", "50 ms", "vs baseline.") or []] == ["hrv_avg_ms"]
    assert [m.key for m in imp._row_metrics("body fat %", "20%", "LBM.") or []] == ["body_fat_pct"]
    assert [m.key for m in imp._row_metrics("sleep consistency", "Fair", ". min") or []] == ["sleep_consistency"]


def test_blood_pressure_with_decimals() -> None:
    got = {m.key: m.value for m in imp._row_metrics("blood pressure", "117.6/70.0 avg", "11 readings") or []}
    assert (got["bp_systolic_mmhg"], got["bp_diastolic_mmhg"]) == (117.6, 70.0)


INLINE_SUPPLEMENT = (
    "## Period of 2026-03-09 – 2026-03-15 (reported 2026-03-15) "
    "| Metric | Value | Notes | |---|---|---| | HRV avg | 50.0 ms | | "
    "**Recommend supplement for period Mar 9-15, 2026** (recommend, 2026-03-16): fired #6 · #18. "
    "**Validated interventions** (cache): | Intervention | Last validated | Verdict | |---|---|---| "
    "| Example timing | 2026-03-01 | supported | "
    "## Period of 2026-03-02 – 2026-03-08 (reported 2026-03-08) | Metric | Value | Notes | |---|---|---| "
    "| HRV avg | 49.0 ms | | "
)


def test_inline_bold_supplement_label_stays_in_its_period() -> None:
    parsed = imp.parse_note(INLINE_SUPPLEMENT)
    assert [n.subject for n in parsed.periods[0].interventions] == ["Example timing"]
    assert not any("supplement" in w for w in parsed.warnings)


def test_bare_table_rows_split_without_parity_and_unknown_labels() -> None:
    line = (
        "Metric | Value | Notes HRV avg | 57.6ms | strong signal. Mystery thing | 3 | x | y "
        "RHR avg | 52.7bpm | ok Protein avg | 126g/day | 5 logged days"
    )
    rows = imp._reflow_bare_table(line)
    assert rows is not None
    labels = [r.split("|")[1].strip() for r in rows[2:]]
    assert labels[0] == "HRV avg"
    assert "Mystery thing" in labels
    assert "RHR avg" in labels and "Protein avg" in labels
    assert imp._row_metrics("protein avg", "126g/day", "")[0].key == "protein_g_day"


def test_legacy_labels_split_and_map() -> None:
    line = (
        "Metric | Value | Notes Body fat % | 26.5% | 1 reading. Every dose early Caffeine | ~142mg/day | ok "
        "Hydration | ~670mL/day | low Protein | ~70g/day | short Magnesium | 327mg/day | diet"
    )
    rows = imp._reflow_bare_table(line)
    assert rows is not None
    labels = [r.split("|")[1].strip() for r in rows[2:]]
    assert labels == ["Body fat %", "Caffeine", "Hydration", "Protein", "Magnesium"]
    keys = {lab: [m.key for m in imp._row_metrics(imp._label(lab), "1 x", "") or []]
            for lab in ("Hydration", "Protein", "Magnesium")}
    assert keys == {"Hydration": ["water_avg_ml_day"], "Protein": ["protein_g_day"], "Magnesium": ["magnesium_mg_day"]}
