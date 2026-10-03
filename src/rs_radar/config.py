"""Runtime settings, resolved from environment variables. Secrets are never written to disk."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from rs_radar.http import HttpClient
from rs_radar.models import Market
from rs_radar.providers.base import MarketDataProvider
from rs_radar.providers.crypto import CryptoProvider
from rs_radar.providers.stocks import StocksProvider
from rs_radar.scanner import Scanner


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    coingecko_api_key: str | None = None
    coingecko_pro: bool = False
    webhook_url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from the environment.

        ``RS_RADAR_HOME``          data directory (default: the platform's user data dir)
        ``COINGECKO_API_KEY``      optional CoinGecko demo key (higher rate limits)
        ``COINGECKO_PRO_API_KEY``  optional CoinGecko paid key (takes precedence)
        ``RS_RADAR_WEBHOOK_URL``   optional Discord/Slack-compatible webhook for bot alerts
        """
        env = os.environ if env is None else env
        home = env.get("RS_RADAR_HOME") or platformdirs.user_data_dir("rs-radar", appauthor=False)
        pro_key = env.get("COINGECKO_PRO_API_KEY") or None
        return cls(
            home=Path(home).expanduser(),
            coingecko_api_key=pro_key or env.get("COINGECKO_API_KEY") or None,
            coingecko_pro=pro_key is not None,
            webhook_url=env.get("RS_RADAR_WEBHOOK_URL") or None,
        )

    @property
    def watchlist_path(self) -> Path:
        return self.home / "watchlist.json"

    @property
    def bot_dir(self) -> Path:
        return self.home / "bot"


def build_provider(market: Market, settings: Settings, http: HttpClient) -> MarketDataProvider:
    if market is Market.CRYPTO:
        return CryptoProvider(
            http, coingecko_api_key=settings.coingecko_api_key, coingecko_pro=settings.coingecko_pro
        )
    return StocksProvider(http)


def build_scanners(settings: Settings, http: HttpClient) -> dict[Market, Scanner]:
    return {m: Scanner(build_provider(m, settings, http)) for m in Market}
