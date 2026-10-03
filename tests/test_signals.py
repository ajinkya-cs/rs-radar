from __future__ import annotations

import math
from dataclasses import replace

import pytest

from rs_radar.models import Instrument, Market
from rs_radar.signals import (
    Thresholds,
    confidence,
    evaluate,
    momentum,
    passes_gates,
    trade_plan,
    trend,
)
from tests.factories import build, market, returns, uptrend

CRYPTO = Thresholds.for_market(Market.CRYPTO)
SOL = Instrument("SOL", Market.CRYPTO, "binance", "SOLUSDT")
SPIKE = [math.log(1.006)] * 5  # +3.0% over 5 bars


def test_idiosyncratic_spike_on_volume_passes_gates() -> None:
    asset, bench = market(tail=SPIKE, bench_tail=[0.0] * 5, tail_volume=6_000)
    m = momentum(asset, bench)

    assert m.return_5m == pytest.approx(3.04, abs=0.01)
    assert m.excess_5m > 2.5
    assert m.zscore_5m > 5
    assert m.volume_ratio == pytest.approx(6.0)
    assert m.breakout
    assert m.persistence == 5
    assert passes_gates(m, CRYPTO)


def test_high_beta_amplifying_benchmark_is_not_a_signal() -> None:
    # Benchmark +1.5%, a beta-2 asset +3%: big raw move, zero relative strength.
    bench_leg = [math.log(1.003)] * 5
    asset, bench = market(beta=2.0, tail=[2 * r for r in bench_leg], bench_tail=bench_leg, tail_volume=6_000)
    m = momentum(asset, bench)

    assert m.return_5m > 2.5
    assert abs(m.excess_5m) < 0.5
    assert not passes_gates(m, CRYPTO)


def test_quiet_volume_fails_gate() -> None:
    asset, bench = market(tail=SPIKE, bench_tail=[0.0] * 5, tail_volume=1_000)
    assert not passes_gates(momentum(asset, bench), CRYPTO)


def test_held_dip_when_benchmark_sells_off() -> None:
    # Benchmark drops ~1.5% then recovers; asset grinds higher throughout.
    bench_leg = [math.log(0.997)] * 5 + [math.log(1.003)] * 5 + [0.0] * 5
    asset_leg = [math.log(1.001)] * 15
    asset, bench = market(tail=asset_leg, bench_tail=bench_leg)
    assert momentum(asset, bench).held_dip


def test_confidence_rewards_confirmations() -> None:
    asset, bench = market(tail=SPIKE, bench_tail=[0.0] * 5, tail_volume=6_000)
    m = momentum(asset, bench)
    strong = confidence(m, CRYPTO)
    assert 70 <= strong <= 100
    weaker = confidence(replace(m, breakout=False, persistence=0), CRYPTO)
    assert weaker == strong - 15


def test_momentum_requires_aligned_history() -> None:
    asset, bench = market()
    with pytest.raises(ValueError, match="aligned"):
        momentum(asset[:-1], bench)
    with pytest.raises(ValueError, match="aligned"):
        momentum(asset[:20], bench[:20])


def test_trend_detects_uptrend_and_extension() -> None:
    candles = uptrend()
    t = trend(candles, candles[-1].close)
    assert t.uptrend
    assert t.ema20 > t.ema50
    assert t.atr > 0
    down = build([-0.004 + r for r in returns(60, seed=3, vol=0.006)], step=14_400)
    assert not trend(down, down[-1].close).uptrend


def test_trade_plan_geometry() -> None:
    candles = uptrend()
    t = trend(candles, candles[-1].close)
    plan = trade_plan(candles[-1].close, t)

    assert plan.stop < plan.entry < plan.take_profit_1 < plan.take_profit_2
    risk = plan.entry - plan.stop
    assert plan.take_profit_1 - plan.entry == pytest.approx(1.5 * risk)
    assert plan.take_profit_2 - plan.entry == pytest.approx(3.0 * risk)
    assert t.atr <= risk <= 2.0 * t.atr  # stop is always 1-2 ATR away
    assert plan.max_entry == pytest.approx(plan.entry * 1.01)


def test_trade_plan_falls_back_to_atr_stop_when_structure_is_far() -> None:
    t = replace(trend(uptrend(), 100.0), atr=2.0, swing_low=80.0)
    plan = trade_plan(100.0, t)
    assert plan.stop_basis == "atr"
    assert plan.stop == pytest.approx(97.0)


def test_evaluate_requires_trend_and_confidence() -> None:
    asset, bench = market(tail=SPIKE, bench_tail=[0.0] * 5, tail_volume=6_000)
    m = momentum(asset, bench)
    candles = uptrend()
    t = trend(candles, candles[-1].close)

    signal = evaluate(SOL, m, t, CRYPTO, min_confidence=50)
    assert signal is not None
    assert signal.plan.entry == m.price  # plan is anchored on the live price
    assert any("broke 60m high" in r for r in signal.reasons)

    assert evaluate(SOL, m, t, CRYPTO, min_confidence=101) is None
    no_trend = replace(t, uptrend=False)
    assert evaluate(SOL, m, no_trend, CRYPTO, min_confidence=0) is None
    stretched = replace(t, extension_atr=4.0)
    assert evaluate(SOL, m, stretched, CRYPTO, min_confidence=0) is None


def test_stock_thresholds_are_tighter_in_absolute_terms() -> None:
    stocks = Thresholds.for_market(Market.STOCKS)
    assert stocks.min_excess_5m < CRYPTO.min_excess_5m
    assert stocks.min_zscore == CRYPTO.min_zscore
