"""The provider contract every market implements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rs_radar.models import Candle, Instrument, Interval, Market


class MarketDataProvider(Protocol):
    """Source of instruments and candles for one market.

    ``benchmark`` is what relative strength is measured against (BTC for crypto, SPY for stocks).
    ``trend_interval`` is the timeframe used for trend filters and trade plans.
    """

    market: Market
    trend_interval: Interval
    supported_intervals: frozenset[Interval]

    async def benchmark(self) -> Instrument: ...

    async def universe(self, size: int) -> list[Instrument]:
        """The top ``size`` instruments of this market that are worth scanning."""
        ...

    async def resolve(self, symbols: Sequence[str]) -> tuple[list[Instrument], list[str]]:
        """Map user symbols to instruments. Returns ``(resolved, unknown_symbols)``."""
        ...

    async def candles(self, instrument: Instrument, interval: Interval, limit: int) -> list[Candle]:
        """Up to ``limit`` most recent candles, oldest first. The last one may still be forming."""
        ...
