"""Per-market watchlists persisted as JSON in the data directory."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from rs_radar.models import Market
from rs_radar.storage import read_json, write_json
from rs_radar.symbols import normalize

MAX_SYMBOLS = 200
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


class Watchlist:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, market: Market) -> list[str]:
        data = read_json(self.path, {})
        return list(data.get(market.value, []))

    def all(self) -> dict[str, list[str]]:
        return {m.value: self.get(m) for m in Market}

    def add(self, market: Market, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        """Add symbols. Returns ``(added, invalid)``; duplicates are ignored silently."""
        current = self.get(market)
        added, invalid = [], []
        for raw in symbols:
            sym = normalize(raw)
            if not _SYMBOL.match(sym):
                invalid.append(raw)
            elif sym not in current and sym not in added:
                added.append(sym)
        if len(current) + len(added) > MAX_SYMBOLS:
            raise ValueError(f"watchlists are capped at {MAX_SYMBOLS} symbols per market")
        if added:
            self._save(market, current + added)
        return added, invalid

    def remove(self, market: Market, symbols: Iterable[str]) -> list[str]:
        current = self.get(market)
        drop = {normalize(s) for s in symbols}
        removed = [s for s in current if s in drop]
        if removed:
            self._save(market, [s for s in current if s not in drop])
        return removed

    def clear(self, market: Market) -> int:
        n = len(self.get(market))
        self._save(market, [])
        return n

    def _save(self, market: Market, symbols: list[str]) -> None:
        data = read_json(self.path, {})
        data[market.value] = symbols
        write_json(self.path, data)
