"""End-to-end tests of the MCP surface, using an in-process client and the synthetic provider."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from rs_radar.config import Settings
from rs_radar.models import Market
from rs_radar.scanner import Scanner
from rs_radar.server import build_server, parse_time
from tests.fakes import NOW, FakeProvider

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "market_overview",
    "relative_strength",
    "top_movers",
    "strength_since_low",
    "find_signals",
    "rsi",
    "trade_plan",
    "watchlist_add",
    "watchlist_remove",
    "watchlist_show",
    "bot_start",
    "bot_stop",
    "bot_status",
    "bot_alerts",
}
EXPECTED_PROMPTS = {"scan", "signals", "rsi", "since-low", "bot-start", "bot-status", "watchlist-from-image"}


@pytest.fixture
def server(tmp_path: Path) -> MCPServer:
    scanners = {m: Scanner(FakeProvider(), clock=lambda: NOW) for m in Market}
    return build_server(Settings(home=tmp_path), scanners)


async def call(server: MCPServer, tool: str, args: dict[str, Any] | None = None) -> Any:
    async with Client(server) as client:
        result = await client.call_tool(tool, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_lists_tools_with_annotations_and_prompts(server: MCPServer) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        prompts = (await client.list_prompts()).prompts
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert {p.name for p in prompts} == EXPECTED_PROMPTS
    by_name = {t.name: t for t in tools}
    assert by_name["market_overview"].annotations is not None
    assert by_name["market_overview"].annotations.read_only_hint is True
    assert all(t.description for t in tools)


async def test_market_overview(server: MCPServer) -> None:
    data = await call(server, "market_overview", {"top": 3})
    assert data["live"] is True
    assert data["benchmark"]["symbol"] == "BTC"
    assert data["unusual_5m_moves"][0]["symbol"] in {"SPIKE", "DOWN"}
    assert len(data["top_gainers_5m"]) <= 3


async def test_find_signals_returns_plan(server: MCPServer) -> None:
    data = await call(server, "find_signals", {"min_confidence": 50})
    [sig] = data["signals"]
    assert sig["symbol"] == "SPIKE"
    plan = sig["plan"]
    assert plan["stop_loss"] < plan["entry"] < plan["take_profit_1"] < plan["take_profit_2"]
    assert "not financial advice" in data["disclaimer"]


async def test_rankings_rsi_and_plan(server: MCPServer) -> None:
    rs = await call(server, "relative_strength", {"window": "5m", "top": 2})
    assert len(rs["rows"]) == 2
    losers = await call(server, "top_movers", {"direction": "losers", "window": "15m"})
    assert losers["rows"][0]["change_pct"]["15m"] <= losers["rows"][-1]["change_pct"]["15m"]
    rsi = await call(server, "rsi", {"symbols": ["spike", "down"]})
    assert [r["symbol"] for r in rsi["readings"]] == ["SPIKE", "DOWN"]
    plan = await call(server, "trade_plan", {"symbol": "spike"})
    assert plan["trend"]["uptrend"] is True
    assert plan["warning"] is None
    since = await call(server, "strength_since_low", {"lookback_minutes": 30})
    assert since["rows"]


async def test_watchlist_round_trip(server: MCPServer) -> None:
    added = await call(server, "watchlist_add", {"symbols": ["spike", "$quiet", "nope"]})
    assert added["added"] == ["SPIKE", "QUIET"]
    assert added["rejected"] == {"NOPE": "unknown symbol"}
    signals = await call(server, "find_signals", {"min_confidence": 50, "scope": "watchlist"})
    assert [s["symbol"] for s in signals["signals"]] == ["SPIKE"]
    removed = await call(server, "watchlist_remove", {"symbols": ["spike"]})
    assert removed["watchlist"] == ["QUIET"]
    assert (await call(server, "watchlist_show"))["crypto"] == ["QUIET"]


async def test_bot_status_when_stopped(server: MCPServer) -> None:
    status = await call(server, "bot_status")
    assert status["running"] is False
    assert (await call(server, "bot_stop"))["stopped"] is False
    assert (await call(server, "bot_alerts"))["alerts"] == []


async def test_invalid_arguments_are_rejected(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("market_overview", {"market": "forex"})
    assert result.is_error


async def test_prompts_render(server: MCPServer) -> None:
    async with Client(server) as client:
        rsi = await client.get_prompt("rsi", {"symbols": "sol, eth", "interval": "1d"})
        image = await client.get_prompt("watchlist-from-image", {"market": "stocks"})
    rsi_text = rsi.messages[0].content.text  # type: ignore[union-attr]
    assert "['sol', 'eth']" in rsi_text
    assert "'1d'" in rsi_text
    assert "watchlist_add" in image.messages[0].content.text  # type: ignore[union-attr]


def test_parse_time() -> None:
    now = datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)
    assert parse_time("12:30", now=now) == datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)
    assert parse_time("20:24", now=now).day == 2  # a future time today means yesterday
    iso = parse_time("2026-10-02T20:24:00+00:00")
    assert iso == datetime(2026, 10, 2, 20, 24, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="isoformat"):
        parse_time("not a time")
