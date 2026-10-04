from fastmcp import FastMCP

from app.mcp.v1.tools import duckdb_reader, manual_log, report_store

mcp_router = FastMCP(name="Main MCP")

mcp_router.mount(duckdb_reader.duckdb_reader_router)
mcp_router.mount(manual_log.manual_log_router)
mcp_router.mount(report_store.report_store_router)
