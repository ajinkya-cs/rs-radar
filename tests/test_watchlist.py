from __future__ import annotations

from pathlib import Path

import pytest

from rs_radar.config import Settings
from rs_radar.models import Market
from rs_radar.storage import read_json, write_json
from rs_radar.watchlist import Watchlist, normalize


def test_add_normalizes_dedupes_and_rejects_garbage(tmp_path: Path) -> None:
    wl = Watchlist(tmp_path / "watchlist.json")
    added, invalid = wl.add(Market.CRYPTO, ["sol", "$SOL", "pepe/usdt", "BTC_USDT", "not a symbol!"])
    assert added == ["SOL", "PEPE", "BTC"]
    assert invalid == ["not a symbol!"]
    assert wl.add(Market.CRYPTO, ["sol"]) == ([], [])


def test_markets_are_independent_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    Watchlist(path).add(Market.STOCKS, ["nvda", "brk-b"])
    reopened = Watchlist(path)
    assert reopened.get(Market.STOCKS) == ["NVDA", "BRK-B"]
    assert reopened.get(Market.CRYPTO) == []
    assert reopened.all() == {"crypto": [], "stocks": ["NVDA", "BRK-B"]}


def test_remove_and_clear(tmp_path: Path) -> None:
    wl = Watchlist(tmp_path / "w.json")
    wl.add(Market.CRYPTO, ["sol", "eth", "zro"])
    assert wl.remove(Market.CRYPTO, ["eth", "doge"]) == ["ETH"]
    assert wl.get(Market.CRYPTO) == ["SOL", "ZRO"]
    assert wl.clear(Market.CRYPTO) == 2
    assert wl.get(Market.CRYPTO) == []


def test_cap(tmp_path: Path) -> None:
    wl = Watchlist(tmp_path / "w.json")
    with pytest.raises(ValueError, match="capped"):
        wl.add(Market.CRYPTO, [f"T{i}" for i in range(201)])


@pytest.mark.parametrize(("raw", "expected"), [("$eth", "ETH"), ("sol/usdc", "SOL"), ("AAPL", "AAPL")])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_storage_survives_corruption(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_json(path, {"a": 1})
    assert read_json(path, None) == {"a": 1}
    path.write_text("{not json")
    assert read_json(path, {"fallback": True}) == {"fallback": True}
    assert not list(tmp_path.glob(".*.tmp"))


def test_settings_from_env(tmp_path: Path) -> None:
    s = Settings.from_env({"RS_RADAR_HOME": str(tmp_path), "COINGECKO_API_KEY": "demo"})
    assert s.home == tmp_path
    assert s.coingecko_api_key == "demo"
    assert not s.coingecko_pro
    pro = Settings.from_env({"COINGECKO_PRO_API_KEY": "pro", "COINGECKO_API_KEY": "demo"})
    assert pro.coingecko_api_key == "pro"
    assert pro.coingecko_pro
    assert Settings.from_env({}).webhook_url is None
