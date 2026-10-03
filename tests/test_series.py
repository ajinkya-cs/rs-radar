from __future__ import annotations

from rs_radar.models import Candle
from rs_radar.series import align, is_contiguous


def bar(ts: int, close: float, volume: float = 1.0) -> Candle:
    return Candle(ts, close, close, close, close, volume)


def test_align_forward_fills_missing_minutes() -> None:
    grid = [0, 60, 120, 180, 240]
    out = align([bar(0, 1), bar(60, 2), bar(180, 3), bar(240, 4)], grid)
    assert out is not None
    assert [c.close for c in out] == [1, 2, 2, 3, 4]
    assert out[2].volume == 0  # synthetic bar carries no volume


def test_align_backfills_before_first_bar() -> None:
    grid = [0, 60, 120, 180, 240]
    out = align([bar(60, 2), bar(120, 3), bar(180, 4), bar(240, 5)], grid)
    assert out is not None
    assert out[0].close == 2


def test_align_rejects_illiquid_series() -> None:
    grid = list(range(0, 600, 60))
    sparse = [bar(0, 1), bar(300, 1)]
    assert align(sparse, grid) is None
    zero_volume = [bar(t, 1, volume=0) for t in grid]
    assert align(zero_volume, grid) is None


def test_is_contiguous_detects_session_gaps() -> None:
    continuous = [bar(t, 1) for t in range(0, 600, 60)]
    assert is_contiguous(continuous, 6, 60)
    gapped = continuous[:-3] + [bar(10_000 + t, 1) for t in range(0, 180, 60)]
    assert not is_contiguous(gapped, 6, 60)
    assert not is_contiguous(continuous[:3], 6, 60)
