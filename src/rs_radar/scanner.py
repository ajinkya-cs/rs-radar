"""Orchestration: fetch a market, align it to its benchmark and compute everything the tools expose."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from rs_radar.errors import UnknownSymbolError, UnsupportedIntervalError
from rs_radar.indicators import pct_change, rsi
from rs_radar.models import Candle, Instrument, Interval, Market
from rs_radar.providers.base import MarketDataProvider
from rs_radar.series import align, is_contiguous
from rs_radar.signals import (
    Momentum,
    Signal,
    Thresholds,
    TradePlan,
    Trend,
    evaluate,
    momentum,
    passes_gates,
    trade_plan,
    trend,
)

HISTORY_BARS = 300
TREND_BARS = 120
STALE_AFTER_S = 600
PRICE_SANITY_PCT = 8.0
WINDOWS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Row:
    instrument: Instrument
    candles: list[Candle]  # 1m bars aligned to the benchmark grid
    returns: dict[str, float]  # raw % change per window
    excess: dict[str, float]  # beta-adjusted % outperformance vs benchmark per window
    momentum: Momentum


@dataclass(frozen=True, slots=True)
class Snapshot:
    market: Market
    benchmark: Instrument
    benchmark_candles: list[Candle]
    benchmark_returns: dict[str, float]
    rows: list[Row]
    skipped: dict[str, str]  # symbol -> reason
    as_of: int  # open time of the latest benchmark bar (unix seconds)
    live: bool  # fresh, contiguous data: short-term momentum is meaningful right now

    def top(self, key: Callable[[Row], float], limit: int, *, reverse: bool = True) -> list[Row]:
        return sorted(self.rows, key=key, reverse=reverse)[:limit]


@dataclass(frozen=True, slots=True)
class SinceRow:
    row: Row
    change: float
    vs_benchmark: float
    beta_adjusted: float
    volume_ratio: float


@dataclass(frozen=True, slots=True)
class SinceResult:
    anchor_ts: int
    benchmark_change: float
    rows: list[SinceRow]


@dataclass(frozen=True, slots=True)
class RsiReading:
    symbol: str
    price: float
    rsi: float

    @property
    def zone(self) -> str:
        if self.rsi >= 70:
            return "overbought"
        if self.rsi <= 30:
            return "oversold"
        return "neutral"


class Scanner:
    def __init__(self, provider: MarketDataProvider, *, clock: Callable[[], float] = time.time) -> None:
        self.provider = provider
        self._clock = clock

    @property
    def market(self) -> Market:
        return self.provider.market

    async def snapshot(self, *, size: int = 200, symbols: Sequence[str] | None = None) -> Snapshot:
        """Fetch and analyse the universe (or ``symbols``) against the benchmark."""
        bench = await self.provider.benchmark()
        skipped: dict[str, str] = {}
        if symbols:
            instruments, unknown = await self.provider.resolve(symbols)
            skipped.update(dict.fromkeys(unknown, "unknown symbol"))
        else:
            instruments = await self.provider.universe(size)

        bench_candles, results = await asyncio.gather(
            self.provider.candles(bench, Interval.M1, HISTORY_BARS),
            asyncio.gather(
                *(_settle(self.provider.candles(i, Interval.M1, HISTORY_BARS)) for i in instruments)
            ),
        )
        grid = [c.ts for c in bench_candles]
        bench_returns = _window_returns(bench_candles)

        rows = []
        for inst, result in zip(instruments, results, strict=True):
            if isinstance(result, BaseException):
                skipped[inst.symbol] = _reason(result)
                continue
            row, reason = self._analyse(inst, result, bench_candles, grid, bench_returns)
            if row is None:
                skipped[inst.symbol] = reason
            else:
                rows.append(row)

        as_of = bench_candles[-1].ts if bench_candles else 0
        live = self._clock() - as_of < STALE_AFTER_S and is_contiguous(bench_candles, 16, 60)
        return Snapshot(self.market, bench, bench_candles, bench_returns, rows, skipped, as_of, live)

    async def signals(self, snap: Snapshot, *, min_confidence: int = 85) -> list[Signal]:
        """High-confidence long setups. Trend data is fetched only for rows that passed the fast gates."""
        if not snap.live:
            return []
        th = Thresholds.for_market(snap.market)
        gated = [r for r in snap.rows if passes_gates(r.momentum, th)]
        trends = await asyncio.gather(
            *(
                _settle(self.provider.candles(r.instrument, self.provider.trend_interval, TREND_BARS))
                for r in gated
            )
        )
        out = []
        for row, candles in zip(gated, trends, strict=True):
            if isinstance(candles, BaseException):
                continue
            try:
                t = trend(candles, row.momentum.price)
            except ValueError:
                continue
            sig = evaluate(row.instrument, row.momentum, t, th, min_confidence)
            if sig is not None:
                out.append(sig)
        return sorted(out, key=lambda s: s.confidence, reverse=True)

    async def plan(self, symbol: str) -> tuple[Instrument, Trend, TradePlan]:
        """Trend context and a long trade plan for one symbol at its current price."""
        inst = await self._resolve_one(symbol)
        candles = await self.provider.candles(inst, self.provider.trend_interval, TREND_BARS)
        latest = await self.provider.candles(inst, Interval.M1, 2)
        price = latest[-1].close if latest else candles[-1].close
        t = trend(candles, price)
        return inst, t, trade_plan(price, t)

    async def rsi(
        self,
        symbols: Sequence[str] | None,
        *,
        interval: Interval = Interval.H4,
        period: int = 14,
        size: int = 50,
    ) -> tuple[list[RsiReading], dict[str, str]]:
        if interval not in self.provider.supported_intervals:
            supported = ", ".join(sorted(i.value for i in self.provider.supported_intervals))
            raise UnsupportedIntervalError(
                f"{interval.value} is not available for {self.market.value}; use {supported}"
            )
        skipped: dict[str, str] = {}
        if symbols:
            instruments, unknown = await self.provider.resolve(symbols)
            skipped.update(dict.fromkeys(unknown, "unknown symbol"))
        else:
            instruments = [await self.provider.benchmark(), *await self.provider.universe(size)]
        limit = max(10 * period, 150)  # Wilder smoothing needs a long warm-up to converge
        results = await asyncio.gather(
            *(_settle(self.provider.candles(i, interval, limit)) for i in instruments)
        )
        readings = []
        for inst, candles in zip(instruments, results, strict=True):
            if isinstance(candles, BaseException):
                skipped[inst.symbol] = _reason(candles)
            elif len(candles) <= period:
                skipped[inst.symbol] = "not enough history"
            else:
                closes = [c.close for c in candles]
                readings.append(RsiReading(inst.symbol, closes[-1], rsi(closes, period)))
        return readings, skipped

    @staticmethod
    def strength_since(snap: Snapshot, *, lookback: int = 60, at: int | None = None) -> SinceResult:
        """Rank performance since a benchmark anchor: its lowest 1m close in ``lookback`` bars, or ``at``."""
        bench = snap.benchmark_candles
        if at is not None:
            idx = next((i for i, c in enumerate(bench) if c.ts >= at), None)
            if idx is None:
                raise ValueError("anchor time is outside the loaded history")
        else:
            lookback = max(1, min(lookback, len(bench) - 1))
            idx = min(range(len(bench) - lookback, len(bench)), key=lambda i: bench[i].close)
        bench_change = pct_change(bench[-1].close, bench[idx].close)
        bars_since = len(bench) - 1 - idx
        out = []
        for row in snap.rows:
            c = row.candles
            change = pct_change(c[-1].close, c[idx].close)
            history = c[: max(idx, 1)]
            base = sum(x.volume for x in history) / len(history)
            recent = sum(x.volume for x in c[idx + 1 :]) / max(bars_since, 1)
            out.append(
                SinceRow(
                    row=row,
                    change=change,
                    vs_benchmark=change - bench_change,
                    beta_adjusted=change - row.momentum.beta * bench_change,
                    volume_ratio=recent / base if base > 0 else 0.0,
                )
            )
        out.sort(key=lambda r: r.beta_adjusted, reverse=True)
        return SinceResult(bench[idx].ts, bench_change, out)

    async def validate(self, symbols: Sequence[str]) -> tuple[list[str], dict[str, str]]:
        """Resolve symbols and prove each has live data. Returns ``(valid_symbols, rejected -> reason)``."""
        instruments, unknown = await self.provider.resolve(symbols)
        rejected = dict.fromkeys(unknown, "unknown symbol")
        results = await asyncio.gather(
            *(_settle(self.provider.candles(i, Interval.D1, 2)) for i in instruments)
        )
        valid = []
        for inst, result in zip(instruments, results, strict=True):
            if isinstance(result, BaseException) or not result:
                rejected[inst.symbol] = _reason(result) if isinstance(result, BaseException) else "no data"
            else:
                valid.append(inst.symbol)
        return valid, rejected

    def _analyse(
        self,
        inst: Instrument,
        candles: list[Candle],
        bench_candles: list[Candle],
        grid: list[int],
        bench_returns: dict[str, float],
    ) -> tuple[Row | None, str]:
        aligned = align(candles, grid)
        if aligned is None:
            return None, "illiquid (too many minutes without trades)"
        price = aligned[-1].close
        if inst.reference_price and abs(pct_change(price, inst.reference_price)) > PRICE_SANITY_PCT:
            return None, "venue price disagrees with reference (ticker collision)"
        try:
            m = momentum(aligned, bench_candles)
        except ValueError:
            return None, "not enough history"
        returns = _window_returns(aligned)
        excess = {k: returns[k] - m.beta * bench_returns[k] for k in returns}
        return Row(inst, aligned, returns, excess, m), ""

    async def _resolve_one(self, symbol: str) -> Instrument:
        resolved, _ = await self.provider.resolve([symbol])
        if not resolved:
            raise UnknownSymbolError(f"{symbol.upper()} is not available in {self.market.value}")
        return resolved[0]


def _window_returns(candles: Sequence[Candle]) -> dict[str, float]:
    return {
        name: pct_change(candles[-1].close, candles[-1 - bars].close)
        for name, bars in WINDOWS.items()
        if len(candles) > bars
    }


async def _settle(awaitable: Awaitable[T]) -> T | Exception:
    try:
        return await awaitable
    except Exception as exc:  # one bad symbol must never sink a whole scan
        return exc


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__
