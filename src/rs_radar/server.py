"""MCP server: exposes the scanner, watchlists and bot as tools, plus slash-command prompts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from rs_radar import __version__, bot, presenters
from rs_radar.config import Settings, build_scanners
from rs_radar.http import HttpClient, quiet_transport_logs
from rs_radar.models import Interval, Market
from rs_radar.scanner import Scanner
from rs_radar.watchlist import Watchlist

MarketArg = Annotated[
    Literal["crypto", "stocks"], Field(description="crypto (vs BTC) or stocks (US, vs SPY)")
]
WindowArg = Annotated[Literal["5m", "15m", "1h", "4h"], Field(description="Lookback window")]
TopArg = Annotated[int, Field(ge=1, le=50, description="Rows to return")]
SymbolsArg = Annotated[
    list[str], Field(min_length=1, max_length=200, description="Tickers, e.g. ['SOL', 'ETH']")
]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

INSTRUCTIONS = """\
rs-radar screens crypto (top 200 by market cap, benchmark BTC) and US large caps (benchmark SPY) for
relative strength and short-term momentum, and can run a background bot that sends desktop
notifications for high-confidence long setups with entry, stop-loss and take-profit levels.

Guidance:
- "vs benchmark" figures are beta-adjusted: a high-beta coin merely amplifying BTC scores ~0.
- Check `live` in results. When false (e.g. US market closed), momentum figures are historical.
- To build a watchlist from a screenshot, read the tickers from the image yourself, then call
  watchlist_add with the symbols.
- Present trade plans as levels, not instructions, and remind the user this is not financial advice.
"""

DISCLAIMER = "Signals are statistical observations, not financial advice. Markets can move against any setup."


def build_server(  # noqa: PLR0915 - a flat list of tool & prompt registrations
    settings: Settings | None = None,
    scanners: Mapping[Market, Scanner] | None = None,
) -> MCPServer:
    settings = settings or Settings.from_env()
    http: HttpClient | None = None
    if scanners is None:
        http = HttpClient()
        scanners = build_scanners(settings, http)
    watchlist = Watchlist(settings.watchlist_path)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if http is not None:
                await http.aclose()

    mcp = MCPServer(
        name="rs-radar",
        title="RS Radar",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/ajinkya-cs/rs-radar",
        lifespan=lifespan,
    )

    def scanner(market: str) -> Scanner:
        return scanners[Market(market)]

    # --- market analysis ------------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def market_overview(market: MarketArg = "crypto", top: TopArg = 8) -> dict[str, Any]:
        """Everything moving right now: benchmark, breadth, unusual 5m moves, top gainers (5m/15m/1h),
        strongest vs benchmark (1h/4h) and biggest losers."""
        snap = await scanner(market).snapshot()
        return presenters.overview(snap, top)

    @mcp.tool(annotations=READ)
    async def relative_strength(
        market: MarketArg = "crypto",
        window: WindowArg = "1h",
        top: TopArg = 10,
        symbols: Annotated[list[str] | None, Field(description="Limit to these symbols")] = None,
    ) -> dict[str, Any]:
        """Rank instruments by beta-adjusted outperformance vs the benchmark over a window."""
        snap = await scanner(market).snapshot(symbols=symbols)
        return presenters.ranking(snap, "excess", window, top)

    @mcp.tool(annotations=READ)
    async def top_movers(
        market: MarketArg = "crypto",
        window: WindowArg = "5m",
        top: TopArg = 10,
        direction: Annotated[Literal["gainers", "losers"], Field(description="Sort direction")] = "gainers",
    ) -> dict[str, Any]:
        """Raw % gainers or losers over a window (not benchmark-adjusted)."""
        snap = await scanner(market).snapshot()
        return presenters.ranking(snap, "returns", window, top, reverse=direction == "gainers")

    @mcp.tool(annotations=READ)
    async def strength_since_low(
        market: MarketArg = "crypto",
        since: Annotated[
            str | None,
            Field(
                description="Anchor time: 'HH:MM' (local, today) or ISO-8601. Default: benchmark's recent low"
            ),
        ] = None,
        lookback_minutes: Annotated[int, Field(ge=5, le=240)] = 60,
        top: TopArg = 15,
    ) -> dict[str, Any]:
        """Who is strongest since the benchmark's recent low (or a given time)? Use during recoveries."""
        snap = await scanner(market).snapshot()
        at = int(parse_time(since).timestamp()) if since else None
        result = Scanner.strength_since(snap, lookback=lookback_minutes, at=at)
        return {**presenters.snapshot_header(snap), **presenters.since(result, top)}

    @mcp.tool(annotations=READ)
    async def find_signals(
        market: MarketArg = "crypto",
        min_confidence: Annotated[int, Field(ge=0, le=100, description="85+ is rare and strong")] = 85,
        scope: Annotated[
            Literal["universe", "watchlist", "both"], Field(description="What to scan")
        ] = "universe",
    ) -> dict[str, Any]:
        """High-confidence long setups right now, each with entry, stop-loss and take-profit levels."""
        sc = scanner(market)
        watch = watchlist.get(Market(market))
        snap = await sc.snapshot(
            symbols=watch if scope == "watchlist" else None,
            include=watch if scope == "both" else (),
        )
        signals = await sc.signals(snap, min_confidence=min_confidence)
        return {
            **presenters.snapshot_header(snap),
            "min_confidence": min_confidence,
            "signals": [presenters.signal(s) for s in signals],
            "disclaimer": DISCLAIMER,
        }

    @mcp.tool(annotations=READ)
    async def rsi(
        market: MarketArg = "crypto",
        symbols: Annotated[list[str] | None, Field(description="Symbols; omit to scan the top 50")] = None,
        interval: Annotated[
            Literal["1h", "4h", "1d"], Field(description="Candle interval (stocks: 1h or 1d)")
        ] = "4h",
        period: Annotated[int, Field(ge=2, le=50)] = 14,
        zone: Annotated[
            Literal["all", "overbought", "oversold"], Field(description="Filter by RSI zone (70/30)")
        ] = "all",
    ) -> dict[str, Any]:
        """Wilder RSI for symbols, or for the top 50 of a market sorted from most overbought."""
        readings, skipped = await scanner(market).rsi(symbols, interval=Interval(interval), period=period)
        rows = sorted(readings, key=lambda r: -r.rsi)
        if zone != "all":
            rows = [r for r in rows if r.zone == zone]
        return {
            "market": market,
            "interval": interval,
            "period": period,
            "readings": [presenters.rsi_reading(r) for r in rows],
            "skipped": skipped,
        }

    @mcp.tool(annotations=READ)
    async def trade_plan(
        symbol: Annotated[str, Field(description="Ticker, e.g. SOL or NVDA")],
        market: MarketArg = "crypto",
    ) -> dict[str, Any]:
        """Trend context and a long trade plan (entry, ATR/structure stop, 1.5R & 3R targets) for a symbol."""
        sc = scanner(market)
        inst, t, p = await sc.plan(symbol)
        return {
            "symbol": inst.symbol,
            "market": market,
            "timeframe": sc.provider.trend_interval.value,
            "trend": presenters.trend(t),
            "plan": presenters.plan(p),
            "warning": None
            if t.uptrend
            else "Not in an uptrend on this timeframe: a long here is counter-trend.",
            "disclaimer": DISCLAIMER,
        }

    # --- watchlist ------------------------------------------------------------------------------

    @mcp.tool(annotations=WRITE_LOCAL)
    async def watchlist_add(symbols: SymbolsArg, market: MarketArg = "crypto") -> dict[str, Any]:
        """Add symbols to a watchlist (each is checked for live data first). Works with tickers read
        from screenshots."""
        valid, rejected = await scanner(market).validate(symbols)
        added, invalid = watchlist.add(Market(market), valid)
        rejected.update(dict.fromkeys(invalid, "invalid symbol"))
        return {"added": added, "rejected": rejected, "watchlist": watchlist.get(Market(market))}

    @mcp.tool(annotations=WRITE_LOCAL)
    async def watchlist_remove(symbols: SymbolsArg, market: MarketArg = "crypto") -> dict[str, Any]:
        """Remove symbols from a watchlist."""
        removed = watchlist.remove(Market(market), symbols)
        return {"removed": removed, "watchlist": watchlist.get(Market(market))}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def watchlist_show() -> dict[str, Any]:
        """Show all watchlists."""
        return watchlist.all()

    # --- bot --------------------------------------------------------------------------------------

    @mcp.tool(annotations=WRITE_LOCAL)
    async def bot_start(
        markets: Annotated[list[Literal["crypto", "stocks"]], Field(min_length=1)] = ["crypto"],  # noqa: B006
        min_confidence: Annotated[int, Field(ge=50, le=100)] = 85,
        scope: Literal["universe", "watchlist", "both"] = "universe",
        interval_seconds: Annotated[int, Field(ge=30, le=3600)] = 60,
        cooldown_minutes: Annotated[int, Field(ge=5, le=1440)] = 120,
    ) -> dict[str, Any]:
        """Start the background bot: it scans every interval and sends a desktop notification (and
        optional webhook) for high-confidence setups. It keeps running after this session ends."""
        config = bot.BotConfig(
            markets=tuple(Market(m) for m in dict.fromkeys(markets)),
            min_confidence=min_confidence,
            scope=scope,
            interval_s=interval_seconds,
            cooldown_s=cooldown_minutes * 60,
        )
        pid = bot.start(settings, config)
        return {"started": True, "pid": pid, "config": config.to_json(), "stop_with": "bot_stop"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    async def bot_stop() -> dict[str, Any]:
        """Stop the background bot."""
        return {"stopped": bot.stop(settings)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def bot_status() -> dict[str, Any]:
        """Is the bot running and healthy? Includes its config, last cycle and recent alerts."""
        return bot.status(settings)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def bot_alerts(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Alerts the bot has sent, newest last."""
        return {"alerts": bot.read_alerts(bot.BotPaths(settings.bot_dir), limit)}

    # --- prompts (slash commands) -----------------------------------------------------------------

    @mcp.prompt(name="scan", title="Scan the market")
    def scan_prompt(market: str = "crypto") -> str:
        """Overview of what's moving and what's strong right now."""
        return (
            f"Call market_overview for market={market!r}. Summarise in this order: the benchmark's move, "
            "breadth, any unusual 5m moves (with volume), the strongest names vs the benchmark, and the "
            "weakest. Use compact tables and end with a two-line read of the market."
        )

    @mcp.prompt(name="signals", title="Find high-confidence setups")
    def signals_prompt(market: str = "crypto", min_confidence: str = "85") -> str:
        """Current high-confidence long setups with entry, SL and TP."""
        return (
            f"Call find_signals with market={market!r} and min_confidence={min_confidence}. "
            "For each signal show symbol, confidence, entry, max entry, stop-loss, TP1, TP2, risk % and "
            "the reasons. If there are none, say so plainly and show the three strongest names from "
            "relative_strength instead."
        )

    @mcp.prompt(name="rsi", title="RSI check")
    def rsi_prompt(symbols: str = "", market: str = "crypto", interval: str = "4h") -> str:
        """RSI for given symbols (comma-separated) or the most overbought/oversold names."""
        target = (
            f"symbols={[s.strip() for s in symbols.split(',') if s.strip()]}" if symbols else "no symbols"
        )
        return (
            f"Call rsi with market={market!r}, interval={interval!r} and {target}. "
            "Show a table sorted by RSI and call out anything overbought (>=70) or oversold (<=30)."
        )

    @mcp.prompt(name="since-low", title="Strength since the low")
    def since_low_prompt(market: str = "crypto", since: str = "") -> str:
        """Who led the bounce since the benchmark's recent low (or a time like 20:24)."""
        anchor = f"since={since!r}" if since else "the default anchor (benchmark's recent low)"
        return (
            f"Call strength_since_low with market={market!r} and {anchor}. Show the leaders, separating "
            "names with volume behind them from names rising on thin volume."
        )

    @mcp.prompt(name="bot-start", title="Start the signal bot")
    def bot_start_prompt(markets: str = "crypto", min_confidence: str = "85", scope: str = "universe") -> str:
        """Start the background notification bot."""
        market_list = [m.strip() for m in markets.split(",") if m.strip()]
        return (
            f"Call bot_start with markets={market_list}, min_confidence={min_confidence} and "
            f"scope={scope!r}. Confirm what it will watch, how often, and how to stop it (bot_stop)."
        )

    @mcp.prompt(name="bot-status", title="Bot status")
    def bot_status_prompt() -> str:
        """Health, configuration and recent alerts of the background bot."""
        return "Call bot_status and report whether the bot is running and healthy, then list recent alerts."

    @mcp.prompt(name="watchlist-from-image", title="Watchlist from a screenshot")
    def watchlist_from_image_prompt(market: str = "crypto") -> str:
        """Extract tickers from an attached image and add them to the watchlist."""
        return (
            "Read every ticker symbol visible in the attached image (ignore prices, percentages and "
            f"quote currencies like USDT). Then call watchlist_add with market={market!r} and those "
            "symbols. Report what was added and anything rejected, and offer to start the bot with "
            "scope='watchlist'."
        )

    return mcp


def parse_time(value: str, *, now: datetime | None = None) -> datetime:
    """Parse 'HH:MM' (local time; yesterday if that's still in the future) or an ISO-8601 timestamp."""
    now = now or datetime.now().astimezone()
    try:
        hh, mm = (int(p) for p in value.strip().split(":"))
    except ValueError:
        parsed = datetime.fromisoformat(value.strip())
        return parsed if parsed.tzinfo else parsed.astimezone()
    anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return anchor - timedelta(days=1) if anchor > now else anchor


def main() -> None:
    quiet_transport_logs()
    build_server().run("stdio")
