"""Signal engine: beta-adjusted relative strength, confidence scoring and trade plans.

The question every signal answers: *did this instrument just move far more than its benchmark explains,
on real volume, inside an established uptrend?* All functions are pure: they take candles and return
values, which keeps the methodology testable and documented in one place (see docs/methodology.md).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from rs_radar.indicators import atr, beta, ema, log_returns, pct_change
from rs_radar.models import Candle, Instrument, Market

WINDOW = 5  # bars in the momentum window (5 x 1m)
BREAKOUT_LOOKBACK = 60  # bars before the window that define the "prior high"
MIN_BARS = BREAKOUT_LOOKBACK + WINDOW + 10
TREND_MIN_BARS = 55


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Hard gates a move must clear before it is scored at all."""

    min_excess_5m: float  # beta-adjusted outperformance vs benchmark, percentage points
    min_return_5m: float  # the instrument must actually be up, not just "falling less"
    min_zscore: float = 3.0  # excess move vs this instrument's own normal 5m excess spread
    min_volume_ratio: float = 2.0  # last 5m volume vs median 5m volume
    max_extension_atr: float = 3.0  # don't chase: max distance above the trend EMA20, in ATRs

    @classmethod
    def for_market(cls, market: Market) -> Thresholds:
        # A 1.5pp 5-minute outperformance is routine for altcoins but extreme for mega-cap stocks.
        if market is Market.STOCKS:
            return cls(min_excess_5m=0.5, min_return_5m=0.4)
        return cls(min_excess_5m=1.5, min_return_5m=1.0)


@dataclass(frozen=True, slots=True)
class Momentum:
    """Short-term behaviour of one instrument relative to its benchmark (1m bars)."""

    price: float
    beta: float
    return_5m: float
    benchmark_return_5m: float
    excess_5m: float
    zscore_5m: float
    volume_ratio: float
    excess_15m: float
    excess_60m: float
    breakout: bool
    held_dip: bool
    persistence: int  # of the last 5 bars, how many beat the benchmark (beta-adjusted)


@dataclass(frozen=True, slots=True)
class Trend:
    """Trend context on the provider's trend timeframe (4h crypto, 1h stocks)."""

    ema20: float
    ema50: float
    atr: float
    swing_low: float
    uptrend: bool
    extension_atr: float


@dataclass(frozen=True, slots=True)
class TradePlan:
    entry: float
    max_entry: float  # don't chase above this
    stop: float
    take_profit_1: float  # 1.5R: take partial, move stop to entry
    take_profit_2: float  # 3R
    risk_pct: float
    stop_basis: str  # "structure" (below recent swing low) or "atr"


@dataclass(frozen=True, slots=True)
class Signal:
    instrument: Instrument
    momentum: Momentum
    trend: Trend
    confidence: int
    plan: TradePlan
    reasons: tuple[str, ...]


def momentum(asset: Sequence[Candle], bench: Sequence[Candle]) -> Momentum:
    """Compute momentum features from equally-sized, time-aligned 1m series (oldest first)."""
    if len(asset) != len(bench) or len(asset) < MIN_BARS:
        raise ValueError(f"need two aligned series of at least {MIN_BARS} bars")
    closes = [c.close for c in asset]
    bcloses = [c.close for c in bench]

    # Beta from history only: the move being evaluated must not bias its own baseline.
    b = beta(log_returns(closes[:-WINDOW]), log_returns(bcloses[:-WINDOW]))

    def ret(series: Sequence[float], n: int) -> float:
        return pct_change(series[-1], series[-1 - n])

    def excess(n: int) -> float:
        return ret(closes, n) - b * ret(bcloses, n)

    excess_5m = excess(WINDOW)

    # How unusual is this outperformance *for this instrument*? Compare to non-overlapping prior windows.
    ends = range(len(closes) - 1 - WINDOW, WINDOW - 1, -WINDOW)
    samples = [
        pct_change(closes[e], closes[e - WINDOW]) - b * pct_change(bcloses[e], bcloses[e - WINDOW])
        for e in ends
    ]
    spread = statistics.pstdev(samples) or 1e-9

    volumes = [sum(c.volume for c in asset[e - WINDOW + 1 : e + 1]) for e in ends]
    baseline = max(statistics.median(volumes), 0.5 * statistics.fmean(volumes))
    recent_volume = sum(c.volume for c in asset[-WINDOW:])
    volume_ratio = recent_volume / baseline if baseline > 0 else 0.0

    # Held the dip: if the benchmark sold off over the last 15 bars, the asset lost < half its beta share.
    start = -3 * WINDOW - 1
    asset_drawdown = pct_change(min(c.low for c in asset[start + 1 :]), closes[start])
    bench_drawdown = pct_change(min(c.low for c in bench[start + 1 :]), bcloses[start])
    held_dip = bench_drawdown <= -0.3 and asset_drawdown >= 0.5 * b * bench_drawdown

    persistence = sum(
        pct_change(closes[i], closes[i - 1]) - b * pct_change(bcloses[i], bcloses[i - 1]) > 0
        for i in range(len(closes) - WINDOW, len(closes))
    )
    prior_high = max(c.high for c in asset[-(BREAKOUT_LOOKBACK + WINDOW) : -WINDOW])

    return Momentum(
        price=closes[-1],
        beta=b,
        return_5m=ret(closes, WINDOW),
        benchmark_return_5m=ret(bcloses, WINDOW),
        excess_5m=excess_5m,
        zscore_5m=excess_5m / spread,
        volume_ratio=volume_ratio,
        excess_15m=excess(15),
        excess_60m=excess(60),
        breakout=closes[-1] > prior_high,
        held_dip=held_dip,
        persistence=persistence,
    )


def passes_gates(m: Momentum, th: Thresholds) -> bool:
    return (
        m.excess_5m >= th.min_excess_5m
        and m.return_5m >= th.min_return_5m
        and m.zscore_5m >= th.min_zscore
        and m.volume_ratio >= th.min_volume_ratio
    )


def confidence(m: Momentum, th: Thresholds) -> int:
    """Score 0-100. Gated moves start around 25; reaching 85+ needs several independent confirmations."""
    score = min(30.0, 10 + (m.zscore_5m - th.min_zscore) * 10)  # statistical surprise
    score += min(20.0, 10 + (m.excess_5m - th.min_excess_5m) / th.min_excess_5m * 10)  # size of the move
    score += min(15.0, 5 + (m.volume_ratio - th.min_volume_ratio) / 3 * 10)  # participation
    score += 10 if m.breakout else 0
    score += 10 if m.excess_15m > 0 and m.excess_60m > 0 else 0  # strength isn't a one-bar blip
    score += 10 if m.held_dip else 0
    score += 5 if m.persistence >= 4 else 0
    return max(0, min(100, round(score)))


def trend(candles: Sequence[Candle], price: float) -> Trend:
    """Trend context from trend-timeframe candles; the last candle may still be forming."""
    if len(candles) < TREND_MIN_BARS:
        raise ValueError(f"need at least {TREND_MIN_BARS} trend candles, got {len(candles)}")
    closes = [c.close for c in candles]
    closed = candles[:-1]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    a = atr(closed)
    return Trend(
        ema20=e20,
        ema50=e50,
        atr=a,
        swing_low=min(c.low for c in closed[-6:]),
        uptrend=price > e20 > e50,
        extension_atr=(price - e20) / a if a > 0 else 0.0,
    )


def trade_plan(price: float, t: Trend, *, atr_multiple: float = 1.5, chase_pct: float = 1.0) -> TradePlan:
    """Long plan: structural stop under the recent swing low when it sits 1-2 ATR away, else 1.5 ATR."""
    stop, basis = price - atr_multiple * t.atr, "atr"
    structural = t.swing_low - 0.25 * t.atr
    if price - 2.0 * t.atr <= structural <= price - 1.0 * t.atr:
        stop, basis = structural, "structure"
    risk = price - stop
    return TradePlan(
        entry=price,
        max_entry=price * (1 + chase_pct / 100),
        stop=stop,
        take_profit_1=price + 1.5 * risk,
        take_profit_2=price + 3.0 * risk,
        risk_pct=risk / price * 100,
        stop_basis=basis,
    )


def evaluate(
    instrument: Instrument, m: Momentum, t: Trend, th: Thresholds, min_confidence: int
) -> Signal | None:
    """A Signal only when every gate passes, the trend is up and not overextended, and confidence is high."""
    if not passes_gates(m, th) or not t.uptrend or t.extension_atr > th.max_extension_atr:
        return None
    score = confidence(m, th)
    if score < min_confidence:
        return None
    return Signal(instrument, m, t, score, trade_plan(m.price, t), explain(m))


def explain(m: Momentum) -> tuple[str, ...]:
    reasons = [
        f"5m {m.return_5m:+.2f}% vs benchmark {m.benchmark_return_5m:+.2f}%",
        f"{m.zscore_5m:.1f} sigma move",
        f"volume {m.volume_ratio:.1f}x",
        "trend up",
    ]
    if m.breakout:
        reasons.append("broke 60m high")
    if m.held_dip:
        reasons.append("held benchmark dip")
    if m.excess_15m > 0 and m.excess_60m > 0:
        reasons.append("leading on 15m & 1h")
    return tuple(reasons)
