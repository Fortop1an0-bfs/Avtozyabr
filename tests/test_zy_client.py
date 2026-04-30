"""HTTP-level tests for ZYClient (cookies persistence, endpoints, retry)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from avtozyabr.poller.client import (
    AuthError,
    RetryableHTTPError,
    ZYClient,
)


@pytest.fixture
async def client(tmp_path: Path) -> ZYClient:
    c = ZYClient(
        base_url="https://goldapple.ru",
        cookies_file=tmp_path / "zy_cookies.json",
        city_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
    )
    await c.start()
    yield c
    await c.close()


# ── Cookie persistence ────────────────────────────────────────────────────────


async def test_set_cookies_persists_to_json(client: ZYClient, tmp_path: Path):
    persisted = client.set_cookies({"sessionid": "abc", "csrftoken": "xyz"})
    assert persisted is True

    target = tmp_path / "zy_cookies.json"
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"sessionid": "abc", "csrftoken": "xyz"}


async def test_set_cookies_extracts_x_auth_marker(client: ZYClient, tmp_path: Path):
    client.set_cookies({"sessionid": "abc", "x_auth_marker": "MARKER123"})
    # Header is set on live client
    assert client._client.headers.get("X-Auth-Marker") == "MARKER123"
    # Persisted file contains marker under x_auth_marker key (not as cookie)
    data = json.loads((tmp_path / "zy_cookies.json").read_text())
    assert data["x_auth_marker"] == "MARKER123"
    assert "x_auth_marker" not in client.export_cookies()


async def test_set_cookies_does_not_mutate_caller(client: ZYClient):
    payload = {"sessionid": "abc", "x_auth_marker": "M"}
    snapshot = dict(payload)
    client.set_cookies(payload)
    assert payload == snapshot


async def test_load_json_cookies_on_start(tmp_path: Path):
    cookies_file = tmp_path / "zy_cookies.json"
    cookies_file.write_text(
        json.dumps({"sessionid": "loaded", "x_auth_marker": "M"}),
        encoding="utf-8",
    )
    c = ZYClient(
        base_url="https://goldapple.ru",
        cookies_file=cookies_file,
        city_id="city-id",
    )
    await c.start()
    try:
        assert c._client.cookies.get("sessionid") == "loaded"
        assert c._client.headers.get("X-Auth-Marker") == "M"
    finally:
        await c.close()


async def test_load_netscape_cookies_back_compat(tmp_path: Path):
    cookies_file = tmp_path / "zy_cookies.txt"
    # Netscape format: domain, flag, path, secure, expiry, name, value
    cookies_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".goldapple.ru\tTRUE\t/\tTRUE\t9999999999\tsessionid\tns_value\n"
        ".goldapple.ru\tTRUE\t/\tTRUE\t9999999999\tcsrftoken\tns_csrf\n",
        encoding="utf-8",
    )
    c = ZYClient(
        base_url="https://goldapple.ru",
        cookies_file=cookies_file,
        city_id="city-id",
    )
    await c.start()
    try:
        assert c._client.cookies.get("sessionid") == "ns_value"
        assert c._client.cookies.get("csrftoken") == "ns_csrf"
    finally:
        await c.close()


# ── add_to_cart endpoint shape ────────────────────────────────────────────────


async def test_add_to_cart_posts_correct_body(client: ZYClient, httpx_mock):
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/cart/item?locale=ru",
        method="POST",
        json={"data": {"cart": {"items": []}}},
    )
    result = await client.add_to_cart(sku="1011000123", qty=1)
    assert result == {"cart": {"items": []}}

    request = httpx_mock.get_request()
    body = json.loads(request.content)
    assert body["sku"] == "1011000123"
    assert body["fiasId"] == "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
    assert body["isPlaid"] is True
    assert body["cartBeautiesStore"] is True
    # qty is omitted when ==1
    assert "qty" not in body


async def test_add_to_cart_sends_qty_when_gt_1(client: ZYClient, httpx_mock):
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/cart/item?locale=ru",
        method="POST",
        json={"data": {}},
    )
    await client.add_to_cart(sku="SKU1", qty=3)
    body = json.loads(httpx_mock.get_request().content)
    assert body["qty"] == 3


async def test_add_to_cart_coerces_int_sku(client: ZYClient, httpx_mock):
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/cart/item?locale=ru",
        method="POST",
        json={"data": {}},
    )
    await client.add_to_cart(sku=42)
    body = json.loads(httpx_mock.get_request().content)
    assert body["sku"] == "42"


# ── Status handling & retry policy ────────────────────────────────────────────


async def test_401_raises_auth_error(client: ZYClient, httpx_mock):
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=401,
    )
    with pytest.raises(AuthError):
        await client.get_user_info()


async def test_403_does_not_retry_propagates(client: ZYClient, httpx_mock):
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=403,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_user_info()
    # Exactly one request — no retry
    assert len(httpx_mock.get_requests()) == 1


async def test_429_retries_then_succeeds(client: ZYClient, httpx_mock, monkeypatch):
    # Speed up tenacity backoff: it awaits asyncio.sleep between attempts.
    import asyncio

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=429,
    )
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=200,
        json={"data": {"id": 12345}},
    )
    info = await client.get_user_info()
    assert info["id"] == 12345
    assert len(httpx_mock.get_requests()) == 2


async def test_500_retries(client: ZYClient, httpx_mock, monkeypatch):
    import tenacity

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _: None)

    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://goldapple.ru/front/api/user/info/full?locale=ru",
        status_code=503,
    )
    with pytest.raises(RetryableHTTPError):
        await client.get_user_info()
    assert len(httpx_mock.get_requests()) == 3  # 3 attempts, all fail


import re

_TICKER_RE = re.compile(r"https://goldapple\.ru/front/api/ticker/getTicker\?.*pageType=favoritesProducts.*")


async def test_is_authenticated_returns_true_on_200(client: ZYClient, httpx_mock):
    httpx_mock.add_response(url=_TICKER_RE, json={"data": {"data": []}})
    assert await client.is_authenticated() is True


async def test_is_authenticated_returns_false_on_403(client: ZYClient, httpx_mock):
    httpx_mock.add_response(url=_TICKER_RE, status_code=403)
    assert await client.is_authenticated() is False


async def test_is_authenticated_returns_false_on_401(client: ZYClient, httpx_mock):
    httpx_mock.add_response(url=_TICKER_RE, status_code=401)
    assert await client.is_authenticated() is False


# ── Cookie rotation persistence ───────────────────────────────────────────────


async def test_rotated_cookies_persist_to_disk(client: ZYClient, tmp_path: Path, httpx_mock):
    """Server rotates an anti-bot cookie via Set-Cookie; next restart must use the new value."""
    client.set_cookies({"sessionid": "old", "gsscw-goldapple": "challenge_v1"})
    httpx_mock.add_response(
        url=re.compile(r"https://goldapple\.ru/front/api/cart\?.*"),
        json={"data": {}},
        headers=[
            ("set-cookie", "gsscw-goldapple=challenge_v2; Path=/; Domain=goldapple.ru"),
            ("set-cookie", "fgsscw-goldapple=fresh_token; Path=/; Domain=goldapple.ru"),
        ],
    )
    await client.get_cart()

    persisted = json.loads((tmp_path / "zy_cookies.json").read_text(encoding="utf-8"))
    assert persisted["gsscw-goldapple"] == "challenge_v2"
    assert persisted["fgsscw-goldapple"] == "fresh_token"
    assert persisted["sessionid"] == "old"


async def test_no_persist_when_cookies_unchanged(client: ZYClient, tmp_path: Path, httpx_mock):
    """No-op writes are skipped — important since persist runs after every successful request."""
    client.set_cookies({"sessionid": "stable"})
    cookies_file = tmp_path / "zy_cookies.json"
    mtime_before = cookies_file.stat().st_mtime_ns

    httpx_mock.add_response(
        url=re.compile(r"https://goldapple\.ru/front/api/cart\?.*"),
        json={"data": {}},
    )
    await client.get_cart()

    assert cookies_file.stat().st_mtime_ns == mtime_before


async def test_persist_on_close_flushes_pending_changes(tmp_path: Path, httpx_mock):
    """Cookies set in memory but never seen by a successful request still survive shutdown."""
    cookies_file = tmp_path / "zy_cookies.json"
    cookies_file.write_text(json.dumps({"sessionid": "initial"}), encoding="utf-8")

    c = ZYClient(
        base_url="https://goldapple.ru",
        cookies_file=cookies_file,
        city_id="city-id",
    )
    await c.start()
    # Simulate cookie set by httpx internals (e.g. mid-request) without a successful 2xx.
    c._client.cookies.set("sessionid", "rotated_in_flight")
    await c.close()

    persisted = json.loads(cookies_file.read_text(encoding="utf-8"))
    assert persisted["sessionid"] == "rotated_in_flight"
