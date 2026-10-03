from __future__ import annotations

import httpx
import pytest

from rs_radar.errors import DataSourceError
from tests.conftest import mock_http

pytestmark = pytest.mark.anyio


async def test_retries_transient_errors_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls < 3 else httpx.Response(200, json={"ok": True})

    async with mock_http(handler) as http:
        assert await http.get_json("https://api.example.com/x") == {"ok": True}
    assert calls == 3


async def test_gives_up_after_retries_with_status() -> None:
    async with mock_http(lambda _: httpx.Response(503), retries=1) as http:
        with pytest.raises(DataSourceError, match="HTTP 503") as info:
            await http.get_json("https://api.example.com/x")
    assert info.value.status == 503


async def test_client_errors_are_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with mock_http(handler) as http:
        with pytest.raises(DataSourceError) as info:
            await http.get_json("https://api.example.com/x")
    assert info.value.status == 404
    assert calls == 1


async def test_network_errors_are_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with mock_http(handler, retries=1) as http:
        with pytest.raises(DataSourceError, match="ConnectError"):
            await http.get_json("https://api.example.com/x")
