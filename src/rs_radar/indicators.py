"""Pure technical-analysis primitives. No I/O, no state: everything here is unit-testable."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from itertools import pairwise

from rs_radar.models import Candle


def pct_change(new: float, old: float) -> float:
    """Percentage change from ``old`` to ``new``."""
    return (new / old - 1.0) * 100.0


def ema(values: Sequence[float], period: int) -> float:
    """Exponential moving average of ``values``, seeded with the SMA of the first ``period`` values."""
    if len(values) < period:
        raise ValueError(f"need at least {period} values, got {len(values)}")
    k = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1.0 - k)
    return out


def rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder's Relative Strength Index (0-100) of the latest close."""
    if len(closes) < period + 1:
        raise ValueError(f"need at least {period + 1} closes, got {len(closes)}")
    deltas = [b - a for a, b in pairwise(closes)]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Wilder's Average True Range."""
    if len(candles) < period + 1:
        raise ValueError(f"need at least {period + 1} candles, got {len(candles)}")
    trs = [
        max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        for prev, c in pairwise(candles)
    ]
    out = sum(trs[:period]) / period
    for tr in trs[period:]:
        out = (out * (period - 1) + tr) / period
    return out


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in pairwise(closes)]


def beta(
    asset: Sequence[float], benchmark: Sequence[float], *, floor: float = 0.3, cap: float = 3.0
) -> float:
    """Beta of ``asset`` returns to ``benchmark`` returns, clamped to ``[floor, cap]``.

    Clamping matters in practice: when the benchmark is nearly flat its variance collapses and raw beta
    estimates explode, which would otherwise wreck every beta-adjusted metric downstream.
    """
    if len(asset) != len(benchmark) or len(asset) < 2:
        raise ValueError("beta needs two equal-length series of at least 2 returns")
    var = statistics.variance(benchmark)
    if var == 0:
        return 1.0
    raw = statistics.covariance(benchmark, asset) / var
    return min(max(raw, floor), cap)
