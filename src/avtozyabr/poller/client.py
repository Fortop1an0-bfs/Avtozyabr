"""
Async HTTP client for goldapple.ru

Real endpoints discovered via DevTools on 2026-04-30:
  - Profile:   GET /front/api/user/info/full?locale=ru
  - Wishlist:  GET /front/api/ticker/getTicker?locale=ru&pageType=favoritesProducts&moduleType=customer&cityId=...
  - Cart GET:  GET /front/api/cart?locale=ru&forceCreate=false&fiasId=...&isPlaid=true&cartBeautiesStore=true
  - Cart POST: POST /front/api/cart  (TODO: intercept from DevTools when adding item)
  - Stock:     bundled inside wishlist/plp response (no separate stock endpoint found yet)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

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
    def __init__(
        self,
        base_url: str,
        cookies_file: Path | None = None,
        city_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookies_file = cookies_file
        self._city_id = city_id
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
        return dict(self._client.cookies) if self._client else {}

    # ── Low-level request ─────────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)),
    )
    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.get(path, **kwargs)
        log.debug("zy_client.get", path=path, status=resp.status_code)
        if resp.status_code == 401:
            raise AuthError(f"Session expired (401) for {path}")
        resp.raise_for_status()
        return resp

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)),
    )
    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.post(path, **kwargs)
        log.debug("zy_client.post", path=path, status=resp.status_code)
        if resp.status_code == 401:
            raise AuthError(f"Session expired (401) for {path}")
        resp.raise_for_status()
        return resp

    # ── goldapple.ru API endpoints ────────────────────────────────────────────

    async def get_user_info(self) -> dict[str, Any]:
        """Returns profile: id, firstName, phone, city, discount, etc."""
        resp = await self._get("/front/api/user/info/full", params={"locale": "ru"})
        return resp.json().get("data", {})

    async def get_wishlist(self) -> list[dict[str, Any]]:
        """
        Fetch favourites product list.
        Response: {"data": {"data": [...]}}  — list of product objects.
        """
        params: dict[str, Any] = {
            "locale": "ru",
            "pageType": "favoritesProducts",
            "moduleType": "customer",
        }
        if self._city_id:
            params["cityId"] = self._city_id
        resp = await self._get("/front/api/ticker/getTicker", params=params)
        outer = resp.json()
        items = outer.get("data", {}).get("data", [])
        return items if isinstance(items, list) else []

    async def get_cart(self) -> dict[str, Any]:
        """Returns full cart with items, totals, quote_id."""
        params: dict[str, Any] = {
            "locale": "ru",
            "forceCreate": "false",
            "isPlaid": "true",
            "cartBeautiesStore": "true",
        }
        if self._city_id:
            params["fiasId"] = self._city_id
        resp = await self._get("/front/api/cart", params=params)
        return resp.json().get("data", {})

    async def get_product_stock(self, product_id: int) -> dict[str, Any]:
        """
        Stock info for a single product.
        TODO: find the real stock endpoint from DevTools (intercept product page XHR).
        Fallback: re-use cart data or wishlist item fields.
        """
        resp = await self._get(
            f"/front/api/products/{product_id}",
            params={"locale": "ru"},
        )
        return resp.json().get("data", {})

    async def add_to_cart(self, product_id: int, qty: int = 1) -> dict[str, Any]:
        """
        Add item to cart.
        TODO: intercept the real POST from DevTools when clicking 'В корзину'.
        Current best guess based on Magento-style goldapple backend.
        """
        resp = await self._post(
            "/front/api/cart",
            params={"locale": "ru"},
            json={"productId": product_id, "qty": qty},
        )
        return resp.json().get("data", {})

    async def get_checkout_url(self) -> str:
        return f"{self._base_url}/cart"

    async def is_authenticated(self) -> bool:
        try:
            info = await self.get_user_info()
            return bool(info.get("id"))
        except Exception:
            return False
