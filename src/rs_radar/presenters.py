"""Turn analytics results into compact, JSON-friendly dicts for tools and the CLI.

Outputs are read by language models, so they favour short keys, rounded numbers and only the fields
that matter for a decision.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from rs_radar.scanner import Row, RsiReading, SinceResult, Snapshot
from rs_radar.signals import Signal, TradePlan, Trend

WINDOW_KEYS = ("5m", "15m", "1h", "4h")


def price(x: float) -> float:
    """Round a price to 6 significant digits (works for BTC and for sub-cent tokens alike)."""
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, max(0, 5 - math.floor(math.log10(abs(x)))))


def pct(x: float) -> float:
    return round(x, 2)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="minutes")


def row(r: Row) -> dict[str, Any]:
    m = r.momentum
    out: dict[str, Any] = {"symbol": r.instrument.symbol, "price": price(m.price)}
    if r.instrument.rank:
        out["rank"] = r.instrument.rank
    out["change_pct"] = {k: pct(v) for k, v in r.returns.items()}
    out["vs_benchmark_pct"] = {k: pct(v) for k, v in r.excess.items()}
    out["beta"] = round(m.beta, 2)
    out["zscore_5m"] = round(m.zscore_5m, 1)
    out["volume_ratio_5m"] = round(m.volume_ratio, 1)
    return out


def snapshot_header(s: Snapshot) -> dict[str, Any]:
    return {
        "market": s.market.value,
        "benchmark": {
            "symbol": s.benchmark.symbol,
            "price": price(s.benchmark_candles[-1].close),
            "change_pct": {k: pct(v) for k, v in s.benchmark_returns.items()},
        },
        "as_of_utc": iso(s.as_of),
        "live": s.live,
        "note": None
        if s.live
        else "Data is not live (market closed or stale): short-term momentum is historical.",
        "scanned": len(s.rows),
        "skipped": len(s.skipped),
    }


def overview(s: Snapshot, top: int) -> dict[str, Any]:
    def ranked(key: str, window: str, reverse: bool = True) -> list[dict[str, Any]]:
        rows = s.top(lambda r: getattr(r, key).get(window, 0.0), top, reverse=reverse)
        return [row(r) for r in rows]

    breadth_15m = sum(r.returns.get("15m", 0) > 0 for r in s.rows)
    beating_15m = sum(r.excess.get("15m", 0) > 0 for r in s.rows)
    unusual = [r for r in s.rows if r.momentum.zscore_5m >= 3 and r.momentum.volume_ratio >= 2]
    return {
        **snapshot_header(s),
        "breadth": {
            "green_15m": f"{breadth_15m}/{len(s.rows)}",
            "beating_benchmark_15m": f"{beating_15m}/{len(s.rows)}",
        },
        "unusual_5m_moves": [row(r) for r in sorted(unusual, key=lambda r: -r.momentum.zscore_5m)[:top]],
        "top_gainers_5m": ranked("returns", "5m"),
        "top_gainers_15m": ranked("returns", "15m"),
        "top_gainers_1h": ranked("returns", "1h"),
        "strongest_vs_benchmark_1h": ranked("excess", "1h"),
        "strongest_vs_benchmark_4h": ranked("excess", "4h"),
        "biggest_losers_15m": ranked("returns", "15m", reverse=False),
    }


def ranking(s: Snapshot, key: str, window: str, top: int, *, reverse: bool = True) -> dict[str, Any]:
    rows = s.top(lambda r: getattr(r, key).get(window, 0.0), top, reverse=reverse)
    return {**snapshot_header(s), "window": window, "rows": [row(r) for r in rows]}


def trend(t: Trend) -> dict[str, Any]:
    return {
        "uptrend": t.uptrend,
        "ema20": price(t.ema20),
        "ema50": price(t.ema50),
        "atr": price(t.atr),
        "atr_pct": pct(t.atr / t.ema20 * 100) if t.ema20 else None,
        "extension_atr": round(t.extension_atr, 1),
    }


def plan(p: TradePlan) -> dict[str, Any]:
    return {
        "entry": price(p.entry),
        "max_entry": price(p.max_entry),
        "stop_loss": price(p.stop),
        "take_profit_1": price(p.take_profit_1),
        "take_profit_2": price(p.take_profit_2),
        "risk_pct": pct(p.risk_pct),
        "stop_basis": p.stop_basis,
        "management": "Take partial profit at TP1 and move the stop to entry; let the rest run to TP2.",
    }


def signal(sig: Signal) -> dict[str, Any]:
    return {
        "symbol": sig.instrument.symbol,
        "confidence": sig.confidence,
        "plan": plan(sig.plan),
        "reasons": list(sig.reasons),
        "momentum": {
            "change_5m_pct": pct(sig.momentum.return_5m),
            "vs_benchmark_5m_pct": pct(sig.momentum.excess_5m),
            "zscore_5m": round(sig.momentum.zscore_5m, 1),
            "volume_ratio_5m": round(sig.momentum.volume_ratio, 1),
        },
        "trend": trend(sig.trend),
    }


def since(result: SinceResult, top: int) -> dict[str, Any]:
    return {
        "anchor_utc": iso(result.anchor_ts),
        "benchmark_change_pct": pct(result.benchmark_change),
        "beating_benchmark": f"{sum(r.vs_benchmark > 0 for r in result.rows)}/{len(result.rows)}",
        "rows": [
            {
                "symbol": r.row.instrument.symbol,
                "change_pct": pct(r.change),
                "vs_benchmark_pct": pct(r.vs_benchmark),
                "beta_adjusted_pct": pct(r.beta_adjusted),
                "beta": round(r.row.momentum.beta, 2),
                "volume_ratio": round(r.volume_ratio, 1),
            }
            for r in result.rows[:top]
        ],
    }


def rsi_reading(r: RsiReading) -> dict[str, Any]:
    return {"symbol": r.symbol, "rsi": round(r.rsi, 1), "zone": r.zone, "price": price(r.price)}
