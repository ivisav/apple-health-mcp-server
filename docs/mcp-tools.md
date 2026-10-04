# MCP Tools

[← Back to README](../README.md)

The Apple Health MCP Server provides a suite of tools for exploring, searching, and analyzing your Apple Health data, both at the raw XML level and in Elasticsearch/ClickHouse:

## XML Tools (`xml_reader`)

| Tool                | Description                                                                                   |
|---------------------|-----------------------------------------------------------------------------------------------|
| `get_xml_structure` | Analyze the structure and metadata of your Apple Health XML export (file size, tags, types).   |
| `search_xml_content`| Search for specific content in the XML file (by attribute value, device, type, etc.).          |
| `get_xml_by_type`   | Extract all records of a specific health record type from the XML file.                        |

## Elasticsearch Tools (`es_reader`)

| Tool                        | Description                                                                                         |
|-----------------------------|-----------------------------------------------------------------------------------------------------|
| `get_health_summary_es`     | Get a summary of all Apple Health data in Elasticsearch (total count, type breakdown, etc.).         |
| `search_health_records_es`  | Flexible search for health records in Elasticsearch with advanced filtering and query options.        |
| `get_statistics_by_type_es` | Get comprehensive statistics (count, min, max, avg, sum) for a specific health record type.          |
| `get_trend_data_es`         | Analyze trends for a health record type over time (daily, weekly, monthly, yearly aggregations).     |
| `search_values_es`          | Search for records with exactly matching values (including text).     |

## ClickHouse Tools (`ch_reader`)

| Tool                        | Description                                                                                         |
|-----------------------------|-----------------------------------------------------------------------------------------------------|
| `get_health_summary_ch`     | Get a summary of all Apple Health data in ClickHouse (total count, type breakdown, etc.).         |
| `search_health_records_ch`  | Flexible search for health records in ClickHouse with advanced filtering and query options.        |
| `get_statistics_by_type_ch` | Get comprehensive statistics (count, min, max, avg, sum) for a specific health record type.          |
| `get_trend_data_ch`         | Analyze trends for a health record type over time (daily, weekly, monthly, yearly aggregations).     |
| `search_values_ch`          | Search for records with exactly matching values (including text).     |

## DuckDB Tools (`duckdb_reader`)

| Tool                        | Description                                                                                         |
|-----------------------------|-----------------------------------------------------------------------------------------------------|
| `get_health_summary_duckdb`     | Get a summary of all Apple Health data in DuckDB (total count, type breakdown, etc.).         |
| `search_health_records_duckdb`  | Flexible search for health records in DuckDB with advanced filtering and query options.        |
| `get_statistics_by_type_duckdb` | Get comprehensive statistics (count, min, max, avg, sum) for a specific health record type.          |
| `get_trend_data_duckdb`         | Analyze trends for a health record type over time (daily, weekly, monthly, yearly aggregations).     |
| `get_sleep_summary_duckdb`      | Per-night sleep stage (Deep/Core/REM/Awake/In Bed) durations.          |
| `get_blood_pressure_summary_duckdb` | Systolic + diastolic (+ heart rate) readings joined into one row per timestamp. |
| `search_values_duckdb`          | Search for records with exactly matching values (including text).     |

All tools are accessible via MCP-compatible clients and can be used with natural language or programmatic queries to explore and analyze your Apple Health data.

## Report Store Tools (`report_store`)

Per-period history written and read by the weekly-health-report skills, stored in a separate
writable DuckDB file (`REPORTS_DUCKDB_FILENAME`, default `data/health_reports.duckdb`). Created
empty on first use and never touched by the importer.

| Tool | What it does |
|------|--------------|
| `start_report_period` | Register/fetch a reporting period (idempotent by `report_generated`); returns `period_id` + previous 2 periods |
| `upsert_report_metrics` | Write metric values for a period (open set of snake_case keys; re-runs overwrite) |
| `add_report_notes` | Store findings / flags / recommendations / validated interventions |
| `get_report_history` | Last N periods × metrics, with average, change vs previous, all-time max/min |
| `get_report_trend` | Week or month series (day-weighted) for chosen metrics |
| `get_report_notes` | Notes across periods (filter by kind, subject, date; latest per subject) |

One-time import of an existing "Weekly Health Baselines" note export:
`uv run scripts/import_baselines_note.py <export.md> --dry-run`, then without `--dry-run`.
