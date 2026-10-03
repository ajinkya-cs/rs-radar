from __future__ import annotations

import pytest

from rs_radar.errors import UnsupportedIntervalError
from rs_radar.models import Interval
from rs_radar.scanner import Scanner
from tests.fakes import NOW, FakeProvider

pytestmark = pytest.mark.anyio


def scanner(now: float = NOW) -> tuple[Scanner, FakeProvider]:
    provider = FakeProvider()
    return Scanner(provider, clock=lambda: now), provider


async def test_snapshot_ranks_and_skips_illiquid() -> None:
    sc, _ = scanner()
    snap = await sc.snapshot()

    assert snap.live
    assert snap.benchmark.symbol == "BTC"
    assert "THIN" in snap.skipped
    assert {r.instrument.symbol for r in snap.rows} == {"SPIKE", "QUIET", "DOWN"}
    top = snap.top(lambda r: r.returns["5m"], 1)[0]
    assert top.instrument.symbol in {"SPIKE", "DOWN"}
    assert set(top.excess) == {"5m", "15m", "1h", "4h"}


async def test_snapshot_reports_unknown_watchlist_symbols() -> None:
    sc, _ = scanner()
    snap = await sc.snapshot(symbols=["spike", "nope"])
    assert [r.instrument.symbol for r in snap.rows] == ["SPIKE"]
    assert snap.skipped == {"NOPE": "unknown symbol"}


async def test_signals_require_trend_and_only_fetch_trend_for_gated_rows() -> None:
    sc, provider = scanner()
    snap = await sc.snapshot()
    signals = await sc.signals(snap, min_confidence=50)

    assert [s.instrument.symbol for s in signals] == ["SPIKE"]  # DOWN has the same spike, but no uptrend
    trend_calls = {sym for sym, interval in provider.calls if interval is Interval.H4}
    assert trend_calls == {"SPIKE", "DOWN"}  # QUIET never passed the fast gates


async def test_no_signals_on_stale_data() -> None:
    sc, _ = scanner(now=NOW + 3600)
    snap = await sc.snapshot()
    assert not snap.live
    assert await sc.signals(snap, min_confidence=0) == []


async def test_strength_since_benchmark_low() -> None:
    sc, _ = scanner()
    snap = await sc.snapshot()
    result = sc.strength_since(snap, lookback=60)
    assert result.rows[0].beta_adjusted >= result.rows[-1].beta_adjusted
    anchored = sc.strength_since(snap, at=snap.benchmark_candles[-10].ts)
    assert anchored.anchor_ts == snap.benchmark_candles[-10].ts
    with pytest.raises(ValueError, match="outside"):
        sc.strength_since(snap, at=NOW + 10_000)


async def test_rsi_readings_and_interval_validation() -> None:
    sc, _ = scanner()
    readings, skipped = await sc.rsi(["spike", "down", "nope"], interval=Interval.H4)
    by_symbol = {r.symbol: r for r in readings}
    assert by_symbol["SPIKE"].rsi > by_symbol["DOWN"].rsi
    assert skipped == {"NOPE": "unknown symbol"}
    with pytest.raises(UnsupportedIntervalError):
        await sc.rsi(["spike"], interval=Interval.H1)


async def test_plan_and_validate() -> None:
    sc, _ = scanner()
    inst, trend, plan = await sc.plan("spike")
    assert inst.symbol == "SPIKE"
    assert trend.uptrend
    assert plan.stop < plan.entry
    valid, rejected = await sc.validate(["spike", "nope"])
    assert valid == ["SPIKE"]
    assert rejected == {"NOPE": "unknown symbol"}
