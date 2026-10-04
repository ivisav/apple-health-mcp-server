import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client

from app.mcp.v1.mcp import mcp_router
from app.services.health import report_store

TOOLS = {
    "start_report_period", "upsert_report_metrics", "add_report_notes",
    "get_report_history", "get_report_trend", "get_report_notes",
}


@pytest.fixture(autouse=True)
def temp_reports_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "health_reports.duckdb"
    monkeypatch.setattr(report_store.store, "path", db_path)
    yield db_path


def _payload(result) -> object:
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_tools_are_mounted() -> None:
    async with Client(mcp_router) as client:
        names = {t.name for t in await client.list_tools()}
    assert TOOLS <= names


@pytest.mark.asyncio
async def test_upsert_description_lists_canonical_keys() -> None:
    async with Client(mcp_router) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "hrv_avg_ms" in (tools["upsert_report_metrics"].description or "")


@pytest.mark.asyncio
async def test_round_trip_through_mcp() -> None:
    async with Client(mcp_router) as client:
        period = _payload(await client.call_tool(
            "start_report_period",
            {"cp_start": "2026-03-09", "cp_end": "2026-03-15", "report_generated": "2026-03-15T10:00:00"},
        ))
        pid = period["period_id"]
        up = _payload(await client.call_tool(
            "upsert_report_metrics",
            {"period_id": pid, "source_skill": "test",
             "metrics": [{"key": "hrv_avg_ms", "value": 50.0}, {"key": "training_status", "value_text": "Productive"}]},
        ))
        assert up["upserted"] == 2
        history = _payload(await client.call_tool("get_report_history", {"last_n": 1}))
        assert history["metrics"]["hrv_avg_ms"]["values"] == [50.0]


@pytest.mark.asyncio
async def test_service_errors_are_returned_not_raised() -> None:
    async with Client(mcp_router) as client:
        res = _payload(await client.call_tool(
            "upsert_report_metrics",
            {"period_id": "00000000-0000-0000-0000-000000000000", "source_skill": "t",
             "metrics": [{"key": "ctl", "value": 1}]},
        ))
    assert "No report period" in res["error"]


@pytest.mark.asyncio
async def test_add_notes_accepts_plain_strings_and_objects() -> None:
    async with Client(mcp_router) as client:
        period = _payload(await client.call_tool(
            "start_report_period",
            {"cp_start": "2026-03-09", "cp_end": "2026-03-15", "report_generated": "2026-03-15T10:00:00"},
        ))
        res = _payload(await client.call_tool(
            "add_report_notes",
            {"period_id": period["period_id"], "kind": "finding", "source_skill": "test",
             "items": ["plain line", {"text": "object line", "subject": "s"}]},
        ))
        assert res["added"] == 2
        notes = _payload(await client.call_tool("get_report_notes", {"kinds": ["finding"]}))
    assert {n["text"] for n in notes} == {"plain line", "object line"}
