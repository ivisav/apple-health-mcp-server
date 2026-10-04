from typing import Any

from fastmcp import FastMCP

from app.schemas.report_store import MetricInput, NoteInput, ReportNoteKind, TrendGranularity
from app.services.health import report_store as svc

report_store_router = FastMCP(name="Report Store MCP")

_CORE_KEYS_DOC = """
Canonical metric keys (reuse these; units are part of the key):
  Sleep/recovery: sleep_score, sleep_consistency (text), sleep_consistency_stddev_min,
    sleep_duration_h, deep_sleep_raw_min, rem_min, nrem_min, recovery_status (0-10),
    training_status (text), body_battery_est
  Autonomic: hrv_avg_ms, hrv_baseline_ms, rhr_avg_bpm, resp_rate_sleep_brpm, spo2_avg_pct,
    vo2max, hrr_1min_bpm
  Body: weight_avg_kg, body_fat_pct, lbm_kg, bp_systolic_mmhg, bp_diastolic_mmhg,
    bp_category (text)
  Activity: steps_avg_day, water_avg_ml_day, stand_h_day, other_activity (text),
    cycling_sessions, total_km, total_elev_m, avg_speed_kmh, avg_cadence_rpm, ctl, atl, tsb
  Nutrition: calories_kcal_day, protein_g_day, magnesium_mg_day, potassium_mg_day,
    alcohol_drinks
New keys are allowed (snake_case, units in the name) — call get_report_history() with no
metrics first to see keys already in use, and reuse them instead of inventing synonyms.
"""


@report_store_router.tool
def start_report_period(
    cp_start: str,
    cp_end: str,
    report_generated: str,
    days: float | None = None,
) -> dict[str, Any]:
    """
    Register (or fetch) the reporting period for a weekly-health-report run.

    Parameters:
    - cp_start, cp_end: ISO dates (YYYY-MM-DD) of the Current Period.
    - report_generated: ISO 8601 timestamp of this orchestrator run. It is the
      period's identity: calling again with the same value returns the same
      period_id (any UTC offset is ignored; stored as local wall-clock time).
    - days: CP length in days (may be fractional). Defaults to cp_end - cp_start + 1.

    Returns period_id (write it into CP_META as PERIOD_ID=), the period fields,
    created (false if it already existed) and previous_periods (up to 2, newest
    first — these are PW and PW2; periods without any metrics are skipped).

    Notes for LLMs:
    - Only the weekly-health-report orchestrator calls this. Sub-skills read
      PERIOD_ID from the CP_META comment in the report HTML instead.
    - Call it once all data is pulled, right before writing the HTML file, then
      write metrics with upsert_report_metrics in the same run.
    """
    try:
        return svc.start_report_period(cp_start, cp_end, report_generated, days)
    except Exception as e:
        return {"error": f"Failed to start report period: {e}"}


def upsert_report_metrics(
    period_id: str,
    metrics: list[MetricInput],
    source_skill: str,
) -> dict[str, Any]:
    """
    Write per-period metric values for a report period. Re-writing the same key
    for the same period overwrites it (safe to re-run a skill).

    Parameters:
    - period_id: from start_report_period or the report's CP_META PERIOD_ID.
    - metrics: list of {key, value?, value_text?, unit?, note?}. Use value for
      numbers and value_text for labels (e.g. training_status "Overreached").
      Skip metrics you don't have — never write placeholders like "—" or 0.
    - source_skill: the skill writing, e.g. "weekly-health-report-detail".

    Returns upserted count and new_keys (keys never seen before and not
    canonical — check them for typos/synonyms).
    """
    try:
        return svc.upsert_report_metrics(period_id, metrics, source_skill)
    except Exception as e:
        return {"error": f"Failed to upsert report metrics: {e}"}


# Append the canonical key list before registering, so the tool description
# the LLM sees includes it (a docstring can't be built with `+` inline).
upsert_report_metrics.__doc__ = (upsert_report_metrics.__doc__ or "") + _CORE_KEYS_DOC
report_store_router.tool(upsert_report_metrics)


@report_store_router.tool
def add_report_notes(
    period_id: str,
    kind: ReportNoteKind,
    items: list[NoteInput | str],
    source_skill: str,
    replace: bool = True,
) -> dict[str, Any]:
    """
    Store short text notes for a report period.

    Parameters:
    - kind: "finding" (Section 3 highlights/concerns, one line each), "flag"
      (active flags), "recommendation" (one per recommendation), or
      "validated_intervention" (recommend's evidence cache: subject = the
      intervention, text = one-line verdict + source, noted_at = date validated).
    - items: list of {text, subject?, noted_at?}; a plain string is shorthand for {text}.
    - replace: true (default) first deletes this skill's notes of this kind for
      this period, so re-runs don't duplicate. Use false to append (e.g. new
      validated interventions).
    """
    try:
        return svc.add_report_notes(period_id, kind, items, source_skill, replace)
    except Exception as e:
        return {"error": f"Failed to add report notes: {e}"}


@report_store_router.tool
def get_report_history(
    metrics: list[str] | None = None,
    last_n: int = 4,
    include_notes: bool = True,
) -> dict[str, Any]:
    """
    Compact history of the last N reported periods — replaces reading the
    "Weekly Health Baselines" Apple Note.

    Parameters:
    - metrics: keys to return; omit for every key present in those periods
      (the response then also lists keys_in_use with how many periods have each).
    - last_n: number of most recent periods (1-52), default 4.
    - include_notes: include the latest period's findings and flags.

    Returns periods (newest first: periods[0] = PW when called before this
    run's start_report_period) and, per metric: unit, direction
    (higher_is_better/lower_is_better/neutral/unknown), values aligned to
    periods (None = not recorded), avg_last_n, delta_vs_prev (periods[0] minus
    periods[1]), all_time_max / all_time_min ({value, cp_end}).

    Notes for LLMs:
    - The latest period's report_generated is the next run's CP_start.
    - Significance thresholds (↑/↓/→) are yours to apply; this returns numbers only.
    - An empty result means no history yet — proceed without it, never block.
    """
    try:
        return svc.get_report_history(metrics, last_n, include_notes)
    except Exception as e:
        return {"error": f"Failed to read report history: {e}"}


@report_store_router.tool
def get_report_trend(
    metrics: list[str],
    granularity: TrendGranularity = "week",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """
    Week-over-week or month-over-month series of numeric report metrics.

    Parameters:
    - metrics: keys to include (text-only metrics are ignored).
    - granularity: "week" (bucket = ISO week of each period's cp_end, "YYYY-Www")
      or "month" ("YYYY-MM" of cp_end).
    - date_from, date_to: optional ISO dates filtering on cp_end.

    Returns buckets (oldest first), n_periods and days per bucket, and series
    {key: [value per bucket]}; each value is the day-weighted mean of the
    periods in that bucket (None = not recorded).
    """
    try:
        return svc.get_report_trend(metrics, granularity, date_from, date_to)
    except Exception as e:
        return {"error": f"Failed to read report trend: {e}"}


@report_store_router.tool
def get_report_notes(
    kinds: list[ReportNoteKind] | None = None,
    subject: str | None = None,
    since: str | None = None,
    latest_per_subject: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Look up report notes across periods, newest first.

    Parameters:
    - kinds: filter by note kind(s).
    - subject: exact subject match, case-insensitive.
    - since: ISO date/timestamp; only notes created at/after it.
    - latest_per_subject: keep only the newest note per (kind, subject) — use for
      the validated-interventions cache (re-search only if older than ~8 weeks).
    - limit: max rows (1-500).

    Use it to find repeated recommendations across periods, too.
    """
    try:
        return svc.get_report_notes(kinds, subject, since, latest_per_subject, limit)
    except Exception as e:
        return [{"error": f"Failed to read report notes: {e}"}]
