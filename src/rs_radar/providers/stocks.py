"""US equities market data via Yahoo Finance's public chart endpoint (free, no API key).

The endpoint is unofficial: it is fine for personal research but has no SLA, so everything that touches
it is isolated here behind the provider contract. Relative strength is measured against SPY.
Yahoo has no 4h bars, so stocks use 1h as their trend timeframe.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rs_radar.errors import DataSourceError, UnknownSymbolError, UnsupportedIntervalError
from rs_radar.http import HttpClient
from rs_radar.models import Candle, Instrument, Interval, Market

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
BENCHMARK = "SPY"

# (yahoo interval, range) per interval, sized to cover what the analytics need with headroom.
_QUERY = {Interval.M1: ("1m", "5d"), Interval.H1: ("60m", "60d"), Interval.D1: ("1d", "2y")}

_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.-][A-Z]{1,2})?$")

# Large-cap US universe: an approximate S&P 100 snapshot, roughly ordered by market cap when written.
# Membership drifts over time; PRs that refresh it are welcome. Ranks are intentionally not exposed.
LARGE_CAPS = tuple(
    """
    NVDA MSFT AAPL GOOGL AMZN META AVGO TSLA BRK-B JPM WMT LLY V ORCL MA NFLX XOM COST JNJ HD
    PLTR PG BAC ABBV CVX KO GE AMD CSCO PM UNH WFC IBM MS GS CRM ABT AXP MCD LIN
    MRK T DIS RTX PEP CAT INTU UBER VZ TMO NOW BKNG QCOM TXN ISRG C BLK SCHW AMGN
    BA SPGI ADBE NEE LOW PFE GILD HON DHR UNP AMAT BMY MU COF DE LMT MDT SBUX ADP
    MO INTC CMCSA GD NKE USB MMM TGT CL CVS DUK SO UPS FDX MDLZ EMR COP AMT PYPL
    """.split()
)


def is_valid_ticker(symbol: str) -> bool:
    return bool(_TICKER.match(symbol))


class StocksProvider:
    market = Market.STOCKS
    trend_interval = Interval.H1
    supported_intervals = frozenset(_QUERY)

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def benchmark(self) -> Instrument:
        return Instrument(BENCHMARK, Market.STOCKS, "yahoo", BENCHMARK, name="SPDR S&P 500 ETF")

    async def universe(self, size: int) -> list[Instrument]:
        if size < 1:
            raise ValueError("stocks universe size must be at least 1")
        return [Instrument(s, Market.STOCKS, "yahoo", s) for s in LARGE_CAPS[:size]]  # clamps to the list

    async def resolve(self, symbols: Sequence[str]) -> tuple[list[Instrument], list[str]]:
        resolved, unknown = [], []
        for raw in symbols:
            sym = raw.strip().upper().replace("/", "-")
            if is_valid_ticker(sym):
                resolved.append(Instrument(sym, Market.STOCKS, "yahoo", sym))
            else:
                unknown.append(raw.strip().upper())
        return resolved, unknown

    async def candles(self, instrument: Instrument, interval: Interval, limit: int) -> list[Candle]:
        if interval not in _QUERY:
            raise UnsupportedIntervalError(
                f"{interval.value} bars are not available for US stocks; use 1m, 1h or 1d"
            )
        yahoo_interval, yahoo_range = _QUERY[interval]
        try:
            payload = await self._http.get_json(
                f"{YAHOO_CHART}/{instrument.venue_symbol}",
                params={"interval": yahoo_interval, "range": yahoo_range},
            )
        except DataSourceError as exc:
            if exc.status == 404:
                raise UnknownSymbolError(f"{instrument.symbol} is not a known US ticker") from exc
            raise
        return parse_chart(payload, instrument.symbol)[-limit:]


def parse_chart(payload: Any, symbol: str) -> list[Candle]:
    """Parse a Yahoo chart payload, skipping the null rows Yahoo emits for minutes with no trades."""
    result = (payload.get("chart") or {}).get("result")
    if not result:
        raise UnknownSymbolError(f"no data for {symbol}")
    series = result[0]
    quote = series["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(series.get("timestamp") or []):
        o, h, low, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if o is None or h is None or low is None or c is None:
            continue
        shares = quote["volume"][i] or 0
        out.append(Candle(int(ts), float(o), float(h), float(low), float(c), float(shares) * float(c)))
    return out
