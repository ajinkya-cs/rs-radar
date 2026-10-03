"""Core value types shared by providers, analytics and the MCP layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    CRYPTO = "crypto"
    STOCKS = "stocks"


class Interval(str, Enum):
    M1 = "1m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return {"1m": 60, "1h": 3600, "4h": 14_400, "1d": 86_400}[self.value]


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar. ``ts`` is the bar's open time (unix seconds); ``volume`` is quote (USD) volume."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable symbol resolved to a concrete data source.

    ``venue`` and ``venue_symbol`` identify where candles come from (e.g. ``binance`` / ``SOLUSDT``).
    ``reference_price`` (from the universe source) guards against ticker collisions, where the same
    symbol on an exchange is a different asset.
    """

    symbol: str
    market: Market
    venue: str
    venue_symbol: str
    rank: int | None = None
    name: str | None = None
    reference_price: float | None = None
