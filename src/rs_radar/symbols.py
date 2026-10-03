"""Symbol normalization: accept the formats people paste or screenshot (``$sol``, ``SOL/USDT``)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_QUOTE_SUFFIX = re.compile(r"[/_-]?(USDT|USDC|USD|PERP)$")


def normalize(symbol: str) -> str:
    """Canonical form: uppercase, ``$`` prefix and pair quote currency stripped (``$sol/usdt`` -> ``SOL``)."""
    s = symbol.strip().upper().lstrip("$")
    if "/" in s or "_" in s:
        return _QUOTE_SUFFIX.sub("", s)
    return s


def normalize_all(symbols: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate, preserving order."""
    return list(dict.fromkeys(n for n in (normalize(s) for s in symbols) if n))
