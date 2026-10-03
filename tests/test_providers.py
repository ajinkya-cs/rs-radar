from __future__ import annotations

from typing import Any

import httpx
import pytest

from rs_radar.errors import UnknownSymbolError, UnsupportedIntervalError
from rs_radar.models import Instrument, Interval, Market
from rs_radar.providers.crypto import CryptoProvider, is_non_directional
from rs_radar.providers.stocks import StocksProvider, is_valid_ticker, parse_chart
from tests.conftest import mock_http

pytestmark = pytest.mark.anyio

CDC_INSTRUMENTS = {
    "result": {
        "data": [
            {
                "inst_type": "CCY_PAIR",
                "tradable": True,
                "base_ccy": "SOL",
                "quote_ccy": "USD",
                "symbol": "SOL_USD",
            },
            {
                "inst_type": "CCY_PAIR",
                "tradable": True,
                "base_ccy": "CRO",
                "quote_ccy": "USD",
                "symbol": "CRO_USD",
            },
            {
                "inst_type": "PERPETUAL_SWAP",
                "tradable": True,
                "base_ccy": "XYZ",
                "quote_ccy": "USD",
                "symbol": "X",
            },
        ]
    }
}
BINANCE_INFO = {
    "symbols": [
        {"baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "symbol": "BTCUSDT"},
        {"baseAsset": "SOL", "quoteAsset": "USDT", "status": "TRADING", "symbol": "SOLUSDT"},
        {"baseAsset": "USDC", "quoteAsset": "USDT", "status": "TRADING", "symbol": "USDCUSDT"},
        {"baseAsset": "WBTC", "quoteAsset": "USDT", "status": "TRADING", "symbol": "WBTCUSDT"},
        {"baseAsset": "OLD", "quoteAsset": "USDT", "status": "BREAK", "symbol": "OLDUSDT"},
    ]
}
COINGECKO_MARKETS = [
    {"symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1, "current_price": 85000},
    {"symbol": "usdc", "name": "USDC", "market_cap_rank": 5, "current_price": 1.0},
    {"symbol": "sol", "name": "Solana", "market_cap_rank": 6, "current_price": 150},
    {"symbol": "wbtc", "name": "Wrapped Bitcoin", "market_cap_rank": 12, "current_price": 85000},
    {"symbol": "cro", "name": "Cronos", "market_cap_rank": 40, "current_price": 0.07},
    {"symbol": "xmr", "name": "Monero", "market_cap_rank": 20, "current_price": 300},
]


def crypto_handler(calls: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/get-instruments"):
            return httpx.Response(200, json=CDC_INSTRUMENTS)
        if path.endswith("/exchangeInfo"):
            return httpx.Response(200, json=BINANCE_INFO)
        if path.endswith("/coins/markets"):
            return httpx.Response(200, json=COINGECKO_MARKETS)
        if path.endswith("/klines"):
            return httpx.Response(200, json=[[60_000, "1", "2", "0.5", "1.5", "10", 0, "15.5"]])
        if path.endswith("/get-candlestick"):
            rows = [
                {"t": 120_000, "o": "2", "h": "3", "l": "1", "c": "2.5", "v": "4"},
                {"t": 60_000, "o": "1", "h": "2", "l": "1", "c": "2", "v": "1"},
            ]
            return httpx.Response(200, json={"result": {"data": rows}})
        return httpx.Response(404)

    return handler


async def test_crypto_universe_filters_and_maps_venues() -> None:
    calls: list[str] = []
    async with mock_http(crypto_handler(calls)) as http:
        provider = CryptoProvider(http)
        universe = await provider.universe(200)
        bench = await provider.benchmark()

    by_symbol = {i.symbol: i for i in universe}
    # BTC is the benchmark, USDC/WBTC are non-directional, XMR is on no supported venue.
    assert set(by_symbol) == {"SOL", "CRO"}
    assert by_symbol["SOL"].venue == "binance"  # Binance wins over Crypto.com
    assert by_symbol["CRO"].venue == "cryptocom"
    assert by_symbol["SOL"].rank == 6
    assert by_symbol["SOL"].reference_price == 150
    assert (bench.symbol, bench.venue_symbol) == ("BTC", "BTCUSDT")
    assert calls.count("/api/v3/exchangeInfo") == 1  # venue map is cached


async def test_crypto_universe_is_cached_until_ttl() -> None:
    calls: list[str] = []
    now = [0.0]
    async with mock_http(crypto_handler(calls)) as http:
        provider = CryptoProvider(http, clock=lambda: now[0])
        await provider.universe(200)
        await provider.universe(200)
        assert calls.count("/api/v3/coins/markets") == 1
        now[0] = 4000.0
        await provider.universe(200)
    assert calls.count("/api/v3/coins/markets") == 2


async def test_crypto_coingecko_key_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return crypto_handler([])(request)  # type: ignore[no-any-return]

    async with mock_http(handler) as http:
        await CryptoProvider(http, coingecko_api_key="demo-key").universe(10)
    assert seen["x-cg-demo-api-key"] == "demo-key"


async def test_crypto_candle_parsing_per_venue() -> None:
    async with mock_http(crypto_handler([])) as http:
        provider = CryptoProvider(http)
        binance = await provider.candles(
            Instrument("SOL", Market.CRYPTO, "binance", "SOLUSDT"), Interval.M1, 5
        )
        cdc = await provider.candles(Instrument("CRO", Market.CRYPTO, "cryptocom", "CRO_USD"), Interval.M1, 5)

    assert binance[0].ts == 60
    assert binance[0].close == 1.5
    assert binance[0].volume == 15.5
    assert [c.ts for c in cdc] == [60, 120]  # sorted oldest first
    assert cdc[1].volume == pytest.approx(4 * 2.5)  # base volume converted to quote


async def test_crypto_resolve_reports_unknown_symbols() -> None:
    async with mock_http(crypto_handler([])) as http:
        resolved, unknown = await CryptoProvider(http).resolve(["sol", " CRO ", "doesnotexist"])
    assert [i.symbol for i in resolved] == ["SOL", "CRO"]
    assert unknown == ["DOESNOTEXIST"]


@pytest.mark.parametrize(
    ("symbol", "name", "expected"),
    [
        ("usdt", "Tether", True),
        ("xyz", "First Digital USD", True),
        ("xyz", "Ethena USDe", True),
        ("abc", "Wrapped Something", True),
        ("sol", "Solana", False),
        ("sushi", "Sushi", False),  # "usd" inside a word must not match
    ],
)
def test_non_directional_detection(symbol: str, name: str, expected: bool) -> None:
    assert is_non_directional(symbol, name) is expected


def yahoo_payload(closes: list[float | None]) -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1000 + 60 * i for i in range(len(closes))],
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": closes,
                                "low": closes,
                                "close": closes,
                                "volume": [100] * len(closes),
                            }
                        ]
                    },
                }
            ]
        }
    }


def test_yahoo_parse_skips_null_rows_and_converts_volume() -> None:
    candles = parse_chart(yahoo_payload([10.0, None, 12.0]), "AAPL")
    assert [c.close for c in candles] == [10.0, 12.0]
    assert candles[1].volume == pytest.approx(1200.0)


def test_yahoo_parse_empty_result_is_unknown_symbol() -> None:
    with pytest.raises(UnknownSymbolError):
        parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}}, "NOPE")


async def test_stocks_unknown_ticker_maps_404() -> None:
    async with mock_http(lambda _: httpx.Response(404)) as http:
        provider = StocksProvider(http)
        with pytest.raises(UnknownSymbolError, match="ZZZZ"):
            await provider.candles(Instrument("ZZZZ", Market.STOCKS, "yahoo", "ZZZZ"), Interval.M1, 10)


async def test_stocks_rejects_unsupported_interval() -> None:
    async with mock_http(lambda _: httpx.Response(200, json=yahoo_payload([1.0]))) as http:
        provider = StocksProvider(http)
        with pytest.raises(UnsupportedIntervalError):
            await provider.candles(await provider.benchmark(), Interval.H4, 10)


async def test_stocks_resolve_and_universe() -> None:
    async with mock_http(lambda _: httpx.Response(500)) as http:
        provider = StocksProvider(http)
        resolved, unknown = await provider.resolve(["nvda", "BRK.B", "brk/b", "not a ticker"])
        universe = await provider.universe(10)
    assert [i.symbol for i in resolved] == ["NVDA", "BRK.B", "BRK-B"]
    assert unknown == ["NOT A TICKER"]
    assert len(universe) == 10


@pytest.mark.parametrize("ticker", ["AAPL", "BRK-B", "BF.B", "T"])
def test_valid_tickers(ticker: str) -> None:
    assert is_valid_ticker(ticker)
