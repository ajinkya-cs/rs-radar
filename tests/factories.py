"""Deterministic synthetic market data for tests."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from rs_radar.models import Candle


def returns(n: int, *, seed: int, vol: float) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, vol) for _ in range(n)]


def build(
    rets: Sequence[float],
    *,
    start: float = 100.0,
    volume: float = 1_000.0,
    step: int = 60,
    t0: int = 1_700_000_000,
    volumes: Sequence[float] | None = None,
) -> list[Candle]:
    """Candles from per-bar log returns; highs/lows hug the open/close so ranges stay realistic."""
    out, price = [], start
    for i, r in enumerate(rets):
        new = price * math.exp(r)
        hi, lo = max(price, new) * 1.0002, min(price, new) * 0.9998
        out.append(Candle(t0 + i * step, price, hi, lo, new, volumes[i] if volumes else volume))
        price = new
    return out


def market(
    n: int = 300,
    *,
    beta: float = 1.2,
    seed: int = 7,
    tail: Sequence[float] = (),
    bench_tail: Sequence[float] = (),
    tail_volume: float = 1_000.0,
) -> tuple[list[Candle], list[Candle]]:
    """(asset, benchmark) where asset = beta * benchmark + noise, then explicit tail returns appended."""
    head = n - max(len(tail), len(bench_tail))
    bench_r = returns(head, seed=seed, vol=0.0006) + list(bench_tail) + [0.0] * (len(tail) - len(bench_tail))
    noise = returns(head, seed=seed + 1, vol=0.0008)
    asset_r = [beta * b + e for b, e in zip(bench_r[:head], noise, strict=True)]
    tail_r = list(tail) + [beta * b for b in bench_r[head + len(tail) :]]
    asset_r += tail_r
    volumes = [1_000.0] * head + [tail_volume] * (n - head)
    return build(asset_r, volumes=volumes), build(bench_r)


def uptrend(n: int = 60, *, drift: float = 0.004, seed: int = 3, step: int = 14_400) -> list[Candle]:
    rets = [drift + r for r in returns(n, seed=seed, vol=0.006)]
    return build(rets, step=step)


def rescale(candles: Sequence[Candle], last_close: float) -> list[Candle]:
    """Scale a series so it ends at ``last_close`` (keeps trend and 1m data on the same price level)."""
    k = last_close / candles[-1].close
    return [Candle(c.ts, c.open * k, c.high * k, c.low * k, c.close * k, c.volume) for c in candles]
