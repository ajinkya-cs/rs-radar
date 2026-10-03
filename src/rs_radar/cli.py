"""Command-line interface. ``rs-radar serve`` is what MCP clients launch; the rest is for terminals."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from rs_radar import __version__, bot, presenters
from rs_radar.config import Settings, build_scanners
from rs_radar.errors import RsRadarError
from rs_radar.http import HttpClient, quiet_transport_logs
from rs_radar.models import Interval, Market
from rs_radar.scanner import Scanner
from rs_radar.watchlist import Watchlist


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    quiet_transport_logs()
    settings = Settings.from_env()
    try:
        args.handler(args, settings)
    except (RsRadarError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - declarative argparse setup
    p = argparse.ArgumentParser(
        prog="rs-radar",
        description="Relative-strength & momentum radar for crypto and US stocks.",
    )
    p.add_argument("--version", action="version", version=f"rs-radar {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def market(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("-m", "--market", choices=[m.value for m in Market], default="crypto")

    def json_flag(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--json", action="store_true", help="print raw JSON")

    sub.add_parser("serve", help="run the MCP server over stdio (used by MCP clients)").set_defaults(
        handler=_serve
    )

    scan = sub.add_parser("scan", help="market overview: movers, strength, breadth")
    market(scan)
    json_flag(scan)
    scan.add_argument("--top", type=int, default=8)
    scan.set_defaults(handler=_scan)

    sig = sub.add_parser("signals", help="current high-confidence long setups")
    market(sig)
    json_flag(sig)
    sig.add_argument("--min-confidence", type=int, default=85)
    sig.set_defaults(handler=_signals)

    rsi = sub.add_parser("rsi", help="RSI for symbols, or the top 50 of a market")
    market(rsi)
    json_flag(rsi)
    rsi.add_argument("symbols", nargs="*")
    rsi.add_argument("-i", "--interval", choices=["1h", "4h", "1d"], default="4h")
    rsi.add_argument("-p", "--period", type=int, default=14)
    rsi.set_defaults(handler=_rsi)

    since = sub.add_parser("since", help="strength since the benchmark's recent low or a given time")
    market(since)
    json_flag(since)
    since.add_argument("--at", help="anchor time, HH:MM (local) or ISO-8601")
    since.add_argument("--lookback", type=int, default=60, help="minutes to search for the low")
    since.add_argument("--top", type=int, default=15)
    since.set_defaults(handler=_since)

    plan = sub.add_parser("plan", help="trend context and a long trade plan for one symbol")
    market(plan)
    json_flag(plan)
    plan.add_argument("symbol")
    plan.set_defaults(handler=_plan)

    wl = sub.add_parser("watchlist", help="manage watchlists")
    wl_sub = wl.add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        cmd = wl_sub.add_parser(action)
        market(cmd)
        cmd.add_argument("symbols", nargs="+")
        cmd.set_defaults(handler=_watchlist)
    wl_sub.add_parser("show").set_defaults(handler=_watchlist)

    b = sub.add_parser("bot", help="background signal bot")
    b_sub = b.add_subparsers(dest="action", required=True)
    start = b_sub.add_parser("start", help="start in the background")
    start.add_argument("-m", "--market", action="append", choices=[m.value for m in Market], dest="markets")
    start.add_argument("--min-confidence", type=int, default=85)
    start.add_argument("--scope", choices=["universe", "watchlist", "both"], default="universe")
    start.add_argument("--interval", type=int, default=60, help="seconds between scans")
    start.add_argument("--cooldown", type=int, default=120, help="minutes before re-alerting a symbol")
    start.set_defaults(handler=_bot)
    for action, text in (
        ("stop", "stop the bot"),
        ("status", "show health and recent alerts"),
        ("run", "run in the foreground"),
        ("alerts", "show recent alerts"),
    ):
        b_sub.add_parser(action, help=text).set_defaults(handler=_bot)
    return p


# --- handlers ----------------------------------------------------------------------------------


def _serve(args: argparse.Namespace, settings: Settings) -> None:
    from rs_radar.server import build_server  # noqa: PLC0415 - keep CLI startup fast

    build_server(settings).run("stdio")


def _with_scanner(market: str, settings: Settings, fn: Callable[[Scanner], Awaitable[Any]]) -> Any:
    async def run() -> Any:
        async with HttpClient() as http:
            return await fn(build_scanners(settings, http)[Market(market)])

    return asyncio.run(run())


def _scan(args: argparse.Namespace, settings: Settings) -> None:
    async def go(sc: Scanner) -> dict[str, Any]:
        return presenters.overview(await sc.snapshot(), args.top)

    data = _with_scanner(args.market, settings, go)
    if args.json:
        return _print_json(data)
    _print_header(data)
    breadth = data["breadth"]
    print(
        f"breadth: {breadth['green_15m']} green on 15m, {breadth['beating_benchmark_15m']} beating benchmark"
    )
    for key in (
        "unusual_5m_moves",
        "top_gainers_5m",
        "top_gainers_15m",
        "strongest_vs_benchmark_1h",
        "strongest_vs_benchmark_4h",
        "biggest_losers_15m",
    ):
        _print_rows(key.replace("_", " "), data[key])
    return None


def _signals(args: argparse.Namespace, settings: Settings) -> None:
    async def go(sc: Scanner) -> dict[str, Any]:
        snap = await sc.snapshot()
        sigs = await sc.signals(snap, min_confidence=args.min_confidence)
        return {**presenters.snapshot_header(snap), "signals": [presenters.signal(s) for s in sigs]}

    data = _with_scanner(args.market, settings, go)
    if args.json:
        return _print_json(data)
    _print_header(data)
    if not data["signals"]:
        print(f"no setups at confidence >= {args.min_confidence} right now")
    for s in data["signals"]:
        p = s["plan"]
        print(
            f"\nLONG {s['symbol']}  {s['confidence']}/100\n"
            f"  entry {p['entry']} (max {p['max_entry']})  SL {p['stop_loss']}  "
            f"TP1 {p['take_profit_1']}  TP2 {p['take_profit_2']}  risk {p['risk_pct']}%\n"
            f"  {', '.join(s['reasons'])}"
        )
    return None


def _rsi(args: argparse.Namespace, settings: Settings) -> None:
    async def go(sc: Scanner) -> dict[str, Any]:
        readings, skipped = await sc.rsi(
            args.symbols or None, interval=Interval(args.interval), period=args.period
        )
        rows = [presenters.rsi_reading(r) for r in sorted(readings, key=lambda r: -r.rsi)]
        return {"readings": rows, "skipped": skipped}

    data = _with_scanner(args.market, settings, go)
    if args.json:
        return _print_json(data)
    _print_table(data["readings"], ["symbol", "rsi", "zone", "price"])
    for sym, reason in data["skipped"].items():
        print(f"skipped {sym}: {reason}")
    return None


def _since(args: argparse.Namespace, settings: Settings) -> None:
    from rs_radar.server import parse_time  # noqa: PLC0415

    async def go(sc: Scanner) -> dict[str, Any]:
        snap = await sc.snapshot()
        at = int(parse_time(args.at).timestamp()) if args.at else None
        result = Scanner.strength_since(snap, lookback=args.lookback, at=at)
        return {**presenters.snapshot_header(snap), **presenters.since(result, args.top)}

    data = _with_scanner(args.market, settings, go)
    if args.json:
        return _print_json(data)
    _print_header(data)
    print(
        f"anchor {data['anchor_utc']} | benchmark {data['benchmark_change_pct']:+.2f}% since | "
        f"{data['beating_benchmark']} beating"
    )
    _print_table(
        data["rows"],
        ["symbol", "change_pct", "vs_benchmark_pct", "beta_adjusted_pct", "beta", "volume_ratio"],
    )
    return None


def _plan(args: argparse.Namespace, settings: Settings) -> None:
    async def go(sc: Scanner) -> dict[str, Any]:
        inst, t, p = await sc.plan(args.symbol)
        return {"symbol": inst.symbol, "trend": presenters.trend(t), "plan": presenters.plan(p)}

    data = _with_scanner(args.market, settings, go)
    if args.json:
        return _print_json(data)
    print(f"{data['symbol']}  trend: {data['trend']}")
    for k, v in data["plan"].items():
        print(f"  {k:>14}: {v}")
    return None


def _watchlist(args: argparse.Namespace, settings: Settings) -> None:
    wl = Watchlist(settings.watchlist_path)
    if args.action == "show":
        return _print_json(wl.all())
    market = Market(args.market)
    if args.action == "add":
        valid, rejected = _with_scanner(args.market, settings, lambda sc: sc.validate(args.symbols))
        added, invalid = wl.add(market, valid)
        rejected.update(dict.fromkeys(invalid, "invalid symbol"))
        _print_json({"added": added, "rejected": rejected, "watchlist": wl.get(market)})
    else:
        _print_json({"removed": wl.remove(market, args.symbols), "watchlist": wl.get(market)})
    return None


def _bot(args: argparse.Namespace, settings: Settings) -> None:
    if args.action == "start":
        config = bot.BotConfig(
            markets=tuple(Market(m) for m in dict.fromkeys(args.markets or ["crypto"])),
            min_confidence=args.min_confidence,
            scope=args.scope,
            interval_s=args.interval,
            cooldown_s=args.cooldown * 60,
        )
        pid = bot.start(settings, config)
        print(f"bot started (pid {pid}); logs: {bot.BotPaths(settings.bot_dir).log}")
    elif args.action == "stop":
        print("bot stopped" if bot.stop(settings) else "bot was not running")
    elif args.action == "status":
        _print_json(bot.status(settings))
    elif args.action == "alerts":
        _print_json(bot.read_alerts(bot.BotPaths(settings.bot_dir), 50))
    else:
        bot.run(settings)


# --- output helpers ------------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _print_header(data: dict[str, Any]) -> None:
    b = data["benchmark"]
    changes = "  ".join(f"{k} {v:+.2f}%" for k, v in b["change_pct"].items())
    print(f"{data['market']} | {b['symbol']} {b['price']} | {changes} | as of {data['as_of_utc']}")
    if not data["live"]:
        print(f"note: {data['note']}")


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n== {title} ==")
    flat = [
        {
            "symbol": r["symbol"],
            "price": r["price"],
            **{f"{k}%": v for k, v in r["change_pct"].items()},
            **{f"vs {k}": v for k, v in r["vs_benchmark_pct"].items()},
            "z5m": r["zscore_5m"],
            "vol5m": r["volume_ratio_5m"],
        }
        for r in rows
    ]
    _print_table(flat, list(flat[0]) if flat else [])


def _print_table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [[_cell(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.rjust(w) for c, w in zip(cols, widths, strict=True)))
    for row in cells:
        print("  ".join(v.rjust(w) for v, w in zip(row, widths, strict=True)))


def _cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return "" if v is None else str(v)
