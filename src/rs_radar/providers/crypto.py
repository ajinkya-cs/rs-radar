"""Crypto market data.

* Universe: CoinGecko ``/coins/markets`` (top N by market cap), minus stablecoins and
  wrapped / staked / bridged duplicates that can't express a directional view.
* Candles: Binance public market data (``data-api.binance.vision``, no key, not geo-blocked), with the
  Crypto.com Exchange public API as fallback for assets Binance doesn't list.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence

from rs_radar.errors import UnknownSymbolError
from rs_radar.http import HttpClient
from rs_radar.models import Candle, Instrument, Interval, Market

COINGECKO_FREE = "https://api.coingecko.com/api/v3"
COINGECKO_PRO = "https://pro-api.coingecko.com/api/v3"
BINANCE = "https://data-api.binance.vision/api/v3"
CRYPTOCOM = "https://api.crypto.com/exchange/v1/public"

BINANCE_MAX_LIMIT = 1000
CRYPTOCOM_MAX_LIMIT = 300
CACHE_TTL_S = 3600.0

# Non-directional assets: stablecoins, tokenised gold/treasuries, wrapped & liquid-staking duplicates.
NON_DIRECTIONAL = frozenset(
    """
    usdt usdc dai fdusd tusd usde usds pyusd usdd frax gusd usdp busd usd0 usdx rlusd usdtb usd1 susde
    susds bfusd usdg usdf usdgo usdai gho crvusd ausd usx ousd reusd apxusd eurc eurs eurcv
    xaut paxg kau kag buidl usyc ustb ousg jtrsy jaaa eutbl usdy syrupusdc
    wbtc weth steth wsteth weeth reth cbbtc cbeth lbtc solvbtc wbeth meth ezeth rseth bnsol jitosol msol
    tbtc clbtc jupsol wbnb wtrx wavax btcb stkaave oseth pufeth ethx lseth
    """.split()
)
_NON_DIRECTIONAL_NAME = re.compile(
    r"\b(wrapped|staked|bridged|restaked|liquid staking|dollar|euro)\b|\busd|usd\b", re.IGNORECASE
)

_INTERVALS = {Interval.M1: "1m", Interval.H1: "1h", Interval.H4: "4h", Interval.D1: "1d"}


def is_non_directional(symbol: str, name: str) -> bool:
    return symbol.lower() in NON_DIRECTIONAL or bool(_NON_DIRECTIONAL_NAME.search(name))


class CryptoProvider:
    market = Market.CRYPTO
    trend_interval = Interval.H4
    supported_intervals = frozenset(_INTERVALS)

    def __init__(
        self,
        http: HttpClient,
        *,
        coingecko_api_key: str | None = None,
        coingecko_pro: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._cg_base = COINGECKO_PRO if coingecko_pro else COINGECKO_FREE
        header = "x-cg-pro-api-key" if coingecko_pro else "x-cg-demo-api-key"
        self._cg_headers = {header: coingecko_api_key} if coingecko_api_key else {}
        self._clock = clock
        self._venues: dict[str, tuple[str, str]] = {}
        self._venues_at = -CACHE_TTL_S
        self._universe: list[Instrument] = []
        self._universe_key = 0
        self._universe_at = -CACHE_TTL_S

    async def benchmark(self) -> Instrument:
        venues = await self._venue_map()
        venue, venue_symbol = venues["btc"]
        return Instrument("BTC", Market.CRYPTO, venue, venue_symbol, rank=1, name="Bitcoin")

    async def universe(self, size: int) -> list[Instrument]:
        if not 1 <= size <= 250:
            raise ValueError("crypto universe size must be between 1 and 250")
        now = self._clock()
        if self._universe_key == size and now - self._universe_at < CACHE_TTL_S:
            return self._universe
        coins = await self._http.get_json(
            f"{self._cg_base}/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": size, "page": 1},
            headers=self._cg_headers,
        )
        venues = await self._venue_map()
        out = []
        for coin in coins:
            sym = str(coin["symbol"]).lower()
            if sym == "btc" or sym not in venues or is_non_directional(sym, str(coin.get("name", ""))):
                continue
            venue, venue_symbol = venues[sym]
            out.append(
                Instrument(
                    symbol=sym.upper(),
                    market=Market.CRYPTO,
                    venue=venue,
                    venue_symbol=venue_symbol,
                    rank=coin.get("market_cap_rank"),
                    name=coin.get("name"),
                    reference_price=coin.get("current_price"),
                )
            )
        self._universe, self._universe_key, self._universe_at = out, size, now
        return out

    async def resolve(self, symbols: Sequence[str]) -> tuple[list[Instrument], list[str]]:
        venues = await self._venue_map()
        resolved, unknown = [], []
        for raw in symbols:
            sym = raw.strip().lower()
            if sym in venues:
                venue, venue_symbol = venues[sym]
                resolved.append(Instrument(sym.upper(), Market.CRYPTO, venue, venue_symbol))
            else:
                unknown.append(raw.strip().upper())
        return resolved, unknown

    async def candles(self, instrument: Instrument, interval: Interval, limit: int) -> list[Candle]:
        tf = _INTERVALS[interval]
        if instrument.venue == "binance":
            rows = await self._http.get_json(
                f"{BINANCE}/klines",
                params={
                    "symbol": instrument.venue_symbol,
                    "interval": tf,
                    "limit": min(limit, BINANCE_MAX_LIMIT),
                },
            )
            return [
                Candle(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]))
                for r in rows
            ]
        if instrument.venue == "cryptocom":
            payload = await self._http.get_json(
                f"{CRYPTOCOM}/get-candlestick",
                params={
                    "instrument_name": instrument.venue_symbol,
                    "timeframe": tf,
                    "count": min(limit, CRYPTOCOM_MAX_LIMIT),
                },
            )
            rows = sorted(payload["result"]["data"], key=lambda r: r["t"])
            return [
                Candle(
                    int(r["t"]) // 1000,
                    float(r["o"]),
                    float(r["h"]),
                    float(r["l"]),
                    float(r["c"]),
                    float(r["v"]) * float(r["c"]),
                )
                for r in rows
            ][-limit:]
        raise UnknownSymbolError(f"unsupported venue {instrument.venue!r} for {instrument.symbol}")

    async def _venue_map(self) -> dict[str, tuple[str, str]]:
        """Lowercase base asset -> (venue, venue symbol). Binance USDT pairs win; Crypto.com fills gaps."""
        now = self._clock()
        if self._venues and now - self._venues_at < CACHE_TTL_S:
            return self._venues
        venues: dict[str, tuple[str, str]] = {}
        cdc = await self._http.get_json(f"{CRYPTOCOM}/get-instruments")
        for inst in cdc["result"]["data"]:
            if (
                inst.get("inst_type") == "CCY_PAIR"
                and inst.get("tradable")
                and inst["quote_ccy"] in ("USD", "USDT")
            ):
                venues.setdefault(str(inst["base_ccy"]).lower(), ("cryptocom", str(inst["symbol"])))
        binance = await self._http.get_json(f"{BINANCE}/exchangeInfo", params={"permissions": "SPOT"})
        for s in binance["symbols"]:
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
                venues[str(s["baseAsset"]).lower()] = ("binance", str(s["symbol"]))
        self._venues, self._venues_at = venues, now
        return venues
