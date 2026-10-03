"""Aligning candle series from different venues onto one time grid."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from rs_radar.models import Candle


def align(
    candles: Sequence[Candle], grid: Sequence[int], *, min_coverage: float = 0.8
) -> list[Candle] | None:
    """Project ``candles`` onto ``grid`` (bar open times), forward-filling gaps.

    Thin markets have no bar for minutes without trades, so series from different venues are not
    index-aligned. Returns ``None`` when fewer than ``min_coverage`` of the grid minutes had real
    trading: momentum computed on a mostly-synthetic series is noise, not signal.
    """
    if not candles or not grid:
        return None
    by_ts = {c.ts: c for c in candles}
    traded = sum(1 for t in grid if t in by_ts and by_ts[t].volume > 0)
    if traded < min_coverage * len(grid):
        return None

    first_ts = min(by_ts)
    prev = next((by_ts[t] for t in sorted(by_ts) if t >= grid[0]), by_ts[first_ts])
    out: list[Candle] = []
    for t in grid:
        bar = by_ts.get(t)
        if bar is None:
            bar = Candle(t, prev.close, prev.close, prev.close, prev.close, 0.0)
        out.append(bar)
        prev = bar
    return out


def is_contiguous(candles: Sequence[Candle], bars: int, step: int) -> bool:
    """True when the last ``bars`` candles are consecutive (no session gap, e.g. a stock market close)."""
    if len(candles) < bars:
        return False
    window = candles[-bars:]
    return all(b.ts - a.ts == step for a, b in pairwise(window))
