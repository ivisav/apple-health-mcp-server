# Getting Started

[← Back to README](../README.md)

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/); Docker is optional.
- Clone and configure:
  ```sh
  git clone https://github.com/ivisav/apple-health-mcp-server
  cd apple-health-mcp-server
  uv sync
  cp config/.env.example config/.env
  ```
  See [Configuration](configuration.md) for the available settings.

## Prepare Your Data

1. Export your Apple Health data on your iPhone and unzip it into the repo
   (`unzip ~/Downloads/export.zip -d .` creates `apple_health_export/export.xml`; git ignores it).
   Set `RAW_XML_PATH="apple_health_export/export.xml"` in `config/.env`.
   - Need sample data? Rob Mulla, *Predict My Sleep Patterns*,
     https://kaggle.com/competitions/kaggle-pog-series-s01e04, 2023 (Kaggle).
2. Import into DuckDB:
   ```sh
   make duckdb
   ```
   Apple Health exports contain your full history every time. The import rebuilds
   `data/applehealth.duckdb` atomically (temp file swapped in on success), so it's always safe to
   re-run: no duplicates, and a failed run leaves the existing database untouched. Restart the MCP
   server afterwards (or wait out the query-cache TTL) to see new data.
   - `make duckdb-reset` deletes `data/applehealth.duckdb` and its `-wal`/temp files.
     `data/manual_logs.duckdb` (fueling log) and `data/health_reports.duckdb` (report history)
     are never touched.

## Connect Your MCP Client

### Local (uv)

Find your uv binary (`which uv` on macOS/Linux, `(Get-Command uv).Path` on Windows), then add:
```json
{
  "mcpServers": {
    "apple-health": {
      "command": "uv",
      "args": ["run", "--frozen", "--directory", "<project-path>", "start"],
      "env": { "PATH": "<folder-containing-uv>" }
    }
  }
}
```

### Docker

Build the image with `make build`, then add (the import still runs on the host with `make duckdb`;
the container only reads `data/`):
```json
{
  "mcpServers": {
    "apple-health": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--init",
        "--mount", "type=bind,source=<project-path>/app,target=/root_project/app",
        "--mount", "type=bind,source=<project-path>/config/.env,target=/root_project/config/.env",
        "--mount", "type=bind,source=<project-path>/data,target=/root_project/data",
        "apple-health-mcp:latest"
      ]
    }
  }
}
```
The `app` mount is optional; it lets code changes apply without rebuilding the image.

Restart your MCP client after changing its config.
