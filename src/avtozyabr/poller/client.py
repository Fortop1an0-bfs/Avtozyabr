"""
Async HTTP client for Zolotoe Yabloko.

Strategy:
  1. Try direct httpx calls with stored cookies/headers.
  2. On 403/captcha, fall back to Playwright browser automation.

All requests go through ZYClient which manages session state.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

# Headers that mimic a real Chrome browser on Windows
_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class AuthError(Exception):
    """Raised when session is expired or cookies are invalid."""


class ZYClient:
    """
    Thin async wrapper around the ZY internal API.

    Cookies can be loaded from:
      - a Netscape-format cookie file (exported from browser)
      - explicitly via set_cookies()
    """

    def __init__(self, base_url: str, cookies_file: Path | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookies_file = cookies_file
        self._client: httpx.AsyncClient | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        cookies = self._load_cookies_from_file() if self._cookies_file else {}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=_BASE_HEADERS,
            cookies=cookies,
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        log.info("zy_client.started", base_url=self._base_url, has_cookies=bool(cookies))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Cookie management ─────────────────────────────────────────────────────

    def _load_cookies_from_file(self) -> dict[str, str]:
        """Parse Netscape cookie file into a simple name→value dict."""
        path = self._cookies_file
        if not path or not path.exists():
            log.warning("zy_client.cookies_file_missing", path=str(path))
            return {}
        cookies: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
        log.info("zy_client.cookies_loaded", count=len(cookies))
        return cookies

    def set_cookies(self, cookies: dict[str, str]) -> None:
        if self._client:
            for name, value in cookies.items():
                self._client.cookies.set(name, value)

    def export_cookies(self) -> dict[str, str]:
        if not self._client:
            return {}
        return dict(self._client.cookies)

    # ── Low-level request ─────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.get(path, **kwargs)
        log.debug("zy_client.get", path=path, status=resp.status_code)
        if resp.status_code == 401:
            raise AuthError(f"Session expired (401) for {path}")
        resp.raise_for_status()
        return resp

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.post(path, **kwargs)
        log.debug("zy_client.post", path=path, status=resp.status_code)
        if resp.status_code == 401:
            raise AuthError(f"Session expired (401) for {path}")
        resp.raise_for_status()
        return resp

    # ── ZY API endpoints ──────────────────────────────────────────────────────

    async def get_wishlist(self) -> list[dict[str, Any]]:
        """
        Fetch wishlist items from the account.

        Returns raw API payload; parsing happens in wishlist.py.
        Endpoint discovered via DevTools — adjust if ZY changes API version.
        """
        resp = await self._get("/api/v1/wishlist")
        data = resp.json()
        # Typical response: {"data": {"items": [...]}} — handle both shapes
        if isinstance(data, list):
            return data
        items = data.get("data", data).get("items", data.get("data", []))
        if isinstance(items, list):
            return items
        return []

    async def get_product_stock(self, product_id: int) -> dict[str, Any]:
        """Fetch stock/availability info for a single product."""
        resp = await self._get(f"/api/v1/products/{product_id}/stock")
        return resp.json()

    async def add_to_cart(self, variant_id: int, qty: int = 1) -> dict[str, Any]:
        """Add a product variant to the shopping cart."""
        resp = await self._post(
            "/api/v1/cart/items",
            json={"variantId": variant_id, "quantity": qty},
        )
        return resp.json()

    async def get_cart(self) -> dict[str, Any]:
        resp = await self._get("/api/v1/cart")
        return resp.json()

    async def get_checkout_url(self) -> str:
        """Return a deep-link URL to the checkout page for use in TG button."""
        return f"{self._base_url}/cart"

    async def is_authenticated(self) -> bool:
        """Probe the profile endpoint to verify session validity."""
        try:
            await self._get("/api/v1/profile")
            return True
        except (AuthError, httpx.HTTPStatusError):
            return False
