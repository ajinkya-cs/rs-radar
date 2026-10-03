"""In-memory MarketDataProvider for tests: synthetic, deterministic, no network."""

from __future__ import annotations

import math
from collections.abc import Sequence

from rs_radar.errors import UnknownSymbolError
from rs_radar.models import Candle, Instrument, Interval, Market
from tests.factories import build, market, rescale, returns, uptrend

NOW = 1_700_000_000 + 300 * 60  # just after the last synthetic 1m bar


class FakeProvider:
    """Benchmark ``BTC`` plus named assets with scripted behaviour.

    ``SPIKE`` breaks out +3% on 6x volume in the last 5 minutes inside a 4h uptrend; ``QUIET`` just
    tracks the benchmark; ``THIN`` trades too rarely to analyse; ``DOWN`` is in a 4h downtrend.
    """

    market = Market.CRYPTO
    trend_interval = Interval.H4
    supported_intervals = frozenset({Interval.M1, Interval.H4, Interval.D1})

    def __init__(self) -> None:
        spike, bench = market(tail=[math.log(1.006)] * 5, bench_tail=[0.0] * 5, tail_volume=6_000)
        quiet, _ = market(seed=7)
        thin = [
            c if i % 5 == 0 else Candle(c.ts, c.open, c.high, c.low, c.close, 0.0)
            for i, c in enumerate(quiet)
        ]
        self.series = {"BTC": bench, "SPIKE": spike, "QUIET": quiet, "THIN": thin, "DOWN": spike}
        down = build([-0.004 + r for r in returns(60, seed=3, vol=0.006)], step=14_400)
        shapes = {"SPIKE": uptrend(), "QUIET": uptrend(), "THIN": uptrend(), "DOWN": down, "BTC": uptrend()}
        self.trend = {sym: rescale(shape, self.series[sym][-1].close) for sym, shape in shapes.items()}
        self.calls: list[tuple[str, Interval]] = []

    def _inst(self, symbol: str) -> Instrument:
        return Instrument(symbol, Market.CRYPTO, "fake", symbol)

    async def benchmark(self) -> Instrument:
        return self._inst("BTC")

    async def universe(self, size: int) -> list[Instrument]:
        return [self._inst(s) for s in ("SPIKE", "QUIET", "THIN", "DOWN")][:size]

    async def resolve(self, symbols: Sequence[str]) -> tuple[list[Instrument], list[str]]:
        known = [s.upper() for s in symbols if s.upper() in self.series]
        unknown = [s.upper() for s in symbols if s.upper() not in self.series]
        return [self._inst(s) for s in known], unknown

    async def candles(self, instrument: Instrument, interval: Interval, limit: int) -> list[Candle]:
        self.calls.append((instrument.symbol, interval))
        if instrument.symbol not in self.series:
            raise UnknownSymbolError(instrument.symbol)
        source = self.series if interval is Interval.M1 else self.trend
        return source[instrument.symbol][-limit:]
