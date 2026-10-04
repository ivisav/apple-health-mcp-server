# About The Project

[← Back to README](../README.md)

**Apple Health MCP Server** is a Model Context Protocol (MCP) server for querying and analyzing
Apple Health data from LLM clients such as Claude Desktop, using natural language instead of SQL.
This fork is based on [the-momentum/apple-health-mcp-server](https://github.com/the-momentum/apple-health-mcp-server)
by [Momentum](https://themomentum.ai) and trims it to a single DuckDB backend, adding a fueling
log and a report store for weekly-report history.

## 🏗️ Architecture

- **Import**: `make duckdb` streams the Apple Health XML export in parallel
  (`scripts/xml_exporter.py`, `scripts/duckdb_importer.py`) and atomically rebuilds
  `data/applehealth.duckdb`.
- **Health queries**: `app/services/health/duckdb_queries.py` reads that database read-only,
  with an in-process TTL/LRU cache; tool arguments are bound as parameters or validated before
  they reach SQL.
- **Writable stores**: the fueling log and report history live in their own DuckDB files
  (`app/services/health/writable_duckdb.py`): short-lived connections, CHECKPOINT on every write,
  created empty on first use, never touched by the importer.
- **MCP layer**: FastMCP routers in `app/mcp/v1/tools/` register the 16 tools; the server runs
  over stdio (`start.py` → `app/main.py`), locally with `uv` or in Docker.
