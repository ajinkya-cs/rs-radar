from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from rs_radar.http import HttpClient

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def mock_http(handler: Handler, *, retries: int = 2) -> HttpClient:
    """An HttpClient whose requests are answered by ``handler`` instead of the network."""
    return HttpClient(transport=httpx.MockTransport(handler), retries=retries, backoff=0)
