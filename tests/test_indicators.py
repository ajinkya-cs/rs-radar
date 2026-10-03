from __future__ import annotations

import math

import pytest

from rs_radar.indicators import atr, beta, ema, log_returns, pct_change, rsi
from rs_radar.models import Candle

# Reference series (unrounded closes) from the StockCharts "RSI" ChartSchool worked example.
STOCKCHARTS_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.2778, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
    45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
]  # fmt: skip


def test_pct_change() -> None:
    assert pct_change(110, 100) == pytest.approx(10.0)
    assert pct_change(90, 100) == pytest.approx(-10.0)


def test_ema_constant_series_is_constant() -> None:
    assert ema([5.0] * 30, 10) == pytest.approx(5.0)


def test_ema_weights_recent_values() -> None:
    assert ema([1.0] * 20 + [2.0] * 5, 10) > ema([1.0] * 20 + [2.0] * 2, 10)


def test_ema_requires_enough_values() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        ema([1.0] * 9, 10)


@pytest.mark.parametrize(
    ("n_closes", "expected"),
    [(15, 70.53), (16, 66.32), (17, 66.55), (18, 69.41), (19, 66.36), (20, 57.97)],
)
def test_rsi_matches_stockcharts_reference(n_closes: int, expected: float) -> None:
    assert rsi(STOCKCHARTS_CLOSES[:n_closes], 14) == pytest.approx(expected, abs=0.01)


def test_rsi_extremes() -> None:
    assert rsi([float(i) for i in range(1, 30)]) == 100.0
    assert rsi([float(i) for i in range(30, 1, -1)]) == pytest.approx(0.0)
    assert rsi([1.0] * 20) == 50.0


def test_atr_of_fixed_range_bars() -> None:
    candles = [Candle(i, 10, 11, 9, 10, 1) for i in range(20)]
    assert atr(candles, 14) == pytest.approx(2.0)


def test_atr_includes_gaps() -> None:
    # A gap up: true range uses the previous close, not just high-low.
    candles = [Candle(i, 10, 10.5, 9.5, 10, 1) for i in range(15)] + [Candle(15, 12, 12.5, 11.5, 12, 1)]
    assert atr(candles, 14) > 1.0


def test_beta_recovers_known_multiplier() -> None:
    bench = [math.sin(i / 3) / 100 for i in range(200)]
    asset = [2 * r for r in bench]
    assert beta(asset, bench) == pytest.approx(2.0)


def test_beta_is_clamped_and_safe_on_flat_benchmark() -> None:
    bench = [math.sin(i) / 1000 for i in range(100)]
    assert beta([50 * r for r in bench], bench) == 3.0
    assert beta([0.01] * 10, [0.0] * 10) == 1.0


def test_log_returns() -> None:
    assert log_returns([100, 110]) == [pytest.approx(math.log(1.1))]
