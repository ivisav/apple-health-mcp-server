# MCP Tools

[← Back to README](../README.md)

The server exposes 16 tools: health-data queries over the imported DuckDB database, a fueling log,
and a report store for weekly-report history.

## DuckDB Tools (`duckdb_reader`)

| Tool | Description |
|------|-------------|
| `get_health_summary_duckdb` | Summary of all imported data (record counts per type). |
| `search_health_records_duckdb` | Search records with filters (type or list of types, source, dates, value range). |
| `get_statistics_by_type_duckdb` | count / min / max / avg / sum for one or more record types. |
| `get_trend_data_duckdb` | Day / week / month / year aggregations for one or more record types. |
| `get_sleep_summary_duckdb` | Per-night sleep stage (Deep/Core/REM/Awake/In Bed) durations. |
| `get_blood_pressure_summary_duckdb` | Systolic + diastolic (+ heart rate) readings joined into one row per timestamp. |
| `search_values_duckdb` | Records with exactly matching values (including text). |

## Fueling Log Tools (`manual_log`)

Stored in a separate writable DuckDB file (`LOGS_DUCKDB_FILENAME`, default `data/manual_logs.duckdb`).

| Tool | Description |
|------|-------------|
| `log_fueling_event` | Log a drink, gel, bar, chew or food item with optional nutrition facts. |
| `search_fueling_events` | Fueling history by date range and category. |
| `delete_fueling_event` | Delete a mis-logged event by id. |

## Report Store Tools (`report_store`)

Per-period history written and read by the weekly-health-report skills, stored in a separate
writable DuckDB file (`REPORTS_DUCKDB_FILENAME`, default `data/health_reports.duckdb`). Created
empty on first use and never touched by the importer.

| Tool | Description |
|------|-------------|
| `start_report_period` | Register/fetch a reporting period (idempotent by `report_generated`); returns `period_id` + previous 2 periods |
| `upsert_report_metrics` | Write metric values for a period (open set of snake_case keys; re-runs overwrite) |
| `add_report_notes` | Store findings / flags / recommendations / validated interventions |
| `get_report_history` | Last N periods × metrics, with average, change vs previous, all-time max/min |
| `get_report_trend` | Week or month series (day-weighted) for chosen metrics |
| `get_report_notes` | Notes across periods (filter by kind, subject, date; latest per subject) |

One-time import of an existing "Weekly Health Baselines" note: see
[README → Report history](../README.md#-report-history).
