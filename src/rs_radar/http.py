"""Shared async HTTP client: bounded concurrency, retries with backoff, consistent errors."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx

from rs_radar import __version__
from rs_radar.errors import DataSourceError

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 10.0


class HttpClient:
    """Thin wrapper over :class:`httpx.AsyncClient` tuned for public market-data APIs.

    Public endpoints rate-limit aggressively, so every request goes through a semaphore and transient
    failures (429/5xx/network) are retried with exponential backoff, honouring ``Retry-After``.
    """

    def __init__(
        self,
        *,
        concurrency: int = 12,
        timeout: float = 15.0,
        retries: int = 3,
        backoff: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"rs-radar/{__version__} (+https://github.com/ajinkya-cs/rs-radar)"},
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retries = retries
        self._backoff = backoff

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        last_error = ""
        last_status: int | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = self._backoff * 2**attempt
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.is_error:
                        raise DataSourceError(_describe(url, response), status=response.status_code)
                    return response.json()
                last_error, last_status = _describe(url, response), response.status_code
                delay = _retry_after(response) or self._backoff * 2**attempt
            if attempt < self._retries:
                await asyncio.sleep(delay)
        raise DataSourceError(
            f"{last_error} (gave up after {self._retries + 1} attempts)", status=last_status
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()


def _describe(url: str, response: httpx.Response) -> str:
    host = httpx.URL(url).host
    return f"{host} returned HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return min(float(value), MAX_RETRY_AFTER_S) if value else None
    except ValueError:
        return None
