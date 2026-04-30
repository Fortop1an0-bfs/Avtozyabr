"""
Async HTTP client for goldapple.ru

Real endpoints discovered via DevTools on 2026-04-30:
  - Profile:   GET  /front/api/user/info/full?locale=ru
  - Wishlist:  GET  /front/api/ticker/getTicker?locale=ru&pageType=favoritesProducts&moduleType=customer&cityId=...
  - Cart GET:  GET  /front/api/cart?locale=ru&forceCreate=false&fiasId=...&isPlaid=true&cartBeautiesStore=true
  - Cart ADD:  POST /front/api/cart/item?locale=ru   body: {sku, fiasId, isPlaid, cartBeautiesStore, ...}
  - Stock:     no dedicated endpoint confirmed yet — wishlist/cart payloads carry availability
"""

from __future__ import annotations

import asyncio
import json
import os
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

# Header name used by goldapple frontend for an auth marker computed in JS.
# If user supplies it via /setcookies JSON under this key, it is promoted to a header.
_AUTH_MARKER_KEY = "x_auth_marker"
_AUTH_MARKER_HEADER = "X-Auth-Marker"


class AuthError(Exception):
    """Raised when session is expired or cookies are invalid (HTTP 401)."""


class RetryableHTTPError(Exception):
    """Raised on 429/5xx responses to trigger tenacity backoff."""


class ZYClient:
    def __init__(
        self,
        base_url: str,
        cookies_file: Path | None = None,
        city_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookies_file = Path(cookies_file) if cookies_file else None
        self._city_id = city_id
        self._client: httpx.AsyncClient | None = None
        # Snapshot of last payload written to disk; used to skip no-op writes.
        self._last_persisted: dict[str, str] = {}
        # Serialize concurrent persists from parallel scheduler jobs.
        self._persist_lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        cookies, marker = self._load_cookies_from_file()
        headers = dict(_BASE_HEADERS)
        if marker:
            headers[_AUTH_MARKER_HEADER] = marker
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            cookies=cookies,
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        # Seed _last_persisted with what's already on disk so the first request
        # only writes if GA actually rotated something.
        snapshot = dict(cookies)
        if marker:
            snapshot[_AUTH_MARKER_KEY] = marker
        self._last_persisted = snapshot
        log.info(
            "zy_client.started",
            base_url=self._base_url,
            has_cookies=bool(cookies),
            has_auth_marker=bool(marker),
        )

    async def close(self) -> None:
        # Final flush so any in-memory cookie deltas survive shutdown.
        try:
            await self._persist_live_state()
        except Exception as exc:
            log.warning("zy_client.persist_on_close_failed", error=str(exc))
        if self._client:
            await self._client.aclose()

    # ── Cookie management ─────────────────────────────────────────────────────

    def _load_cookies_from_file(self) -> tuple[dict[str, str], str | None]:
        """
        Load cookies from `cookies_file`. Format auto-detected:
          - .json  → flat object {name: value, ..., x_auth_marker?: "..."}
          - .txt   → Netscape (7 tab-separated columns; cols 5+6 are name/value)
        Returns (cookies, x_auth_marker).
        """
        path = self._cookies_file
        if not path or not path.exists():
            log.warning("zy_client.cookies_file_missing", path=str(path) if path else None)
            return {}, None

        text = path.read_text(encoding="utf-8")
        marker: str | None = None

        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                log.error("zy_client.cookies_json_invalid", path=str(path), error=str(exc))
                return {}, None
            if not isinstance(data, dict):
                log.error("zy_client.cookies_json_not_object", path=str(path))
                return {}, None
            marker = data.pop(_AUTH_MARKER_KEY, None)
            cookies = {str(k): str(v) for k, v in data.items()}
        else:
            cookies = {}
            for line in text.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]

        log.info("zy_client.cookies_loaded", count=len(cookies), format=path.suffix)
        return cookies, marker

    def _save_cookies_to_file(self, cookies: dict[str, str], marker: str | None) -> None:
        """Persist cookies (+optional auth marker) as JSON for restart-survival."""
        if not self._cookies_file:
            return
        path = self._cookies_file
        # Always write JSON regardless of configured extension — JSON is the canonical
        # interchange format used by /setcookies. Loader tolerates both formats.
        target = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
        payload: dict[str, str] = dict(cookies)
        if marker:
            payload[_AUTH_MARKER_KEY] = marker
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + rename, so a SIGKILL mid-write can't truncate the file.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        log.info("zy_client.cookies_persisted", path=str(target), count=len(cookies))

    async def _persist_live_state(self) -> None:
        """
        Write current in-memory cookies + auth marker back to disk if anything
        changed since last persist. GA rotates anti-bot tokens (gsscw/cfidsw/__zzatw)
        on each response; without this, a restart reverts the session to the
        challenge already burnt by the previous run → 403.
        """
        if not self._client or not self._cookies_file:
            return
        async with self._persist_lock:
            # httpx.Cookies keys by (name, domain, path); iter via the underlying
            # CookieJar so a server-rotated cookie (with a Domain attribute) wins
            # over the original domain-less set_cookies value.
            current: dict[str, str] = {}
            for cookie in self._client.cookies.jar:
                current[cookie.name] = cookie.value
            marker = self._client.headers.get(_AUTH_MARKER_HEADER)
            payload = dict(current)
            if marker:
                payload[_AUTH_MARKER_KEY] = str(marker)
            if payload == self._last_persisted:
                return
            self._save_cookies_to_file(current, str(marker) if marker else None)
            self._last_persisted = payload

    def set_cookies(self, cookies: dict[str, str], persist: bool = True) -> bool:
        """
        Apply cookies to the live client. If `cookies` contains the special
        `x_auth_marker` key, it becomes the X-Auth-Marker header instead.
        With persist=True, the same payload is written to cookies_file so a
        container restart does not lose the session. Returns True if persistence
        actually wrote a file, False otherwise.
        """
        # Don't mutate caller's dict
        local = dict(cookies)
        marker = local.pop(_AUTH_MARKER_KEY, None)
        if self._client:
            for name, value in local.items():
                self._client.cookies.set(name, str(value))
            if marker:
                self._client.headers[_AUTH_MARKER_HEADER] = str(marker)
        if persist and self._cookies_file:
            self._save_cookies_to_file(local, marker)
            # Sync snapshot so the next live-persist doesn't rewrite identical content.
            snapshot = dict(local)
            if marker:
                snapshot[_AUTH_MARKER_KEY] = marker
            self._last_persisted = snapshot
            return True
        return False

    def export_cookies(self) -> dict[str, str]:
        return dict(self._client.cookies) if self._client else {}

    # ── Low-level request ─────────────────────────────────────────────────────

    _RETRY_KWARGS = dict(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
                RetryableHTTPError,
            )
        ),
        reraise=True,
    )

    @staticmethod
    def _check_status(resp: httpx.Response, path: str) -> None:
        if resp.status_code == 401:
            raise AuthError(f"Session expired (401) for {path}")
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise RetryableHTTPError(f"{resp.status_code} for {path}")
        # 403 is treated as a hard failure (auth/bot-detection); do not retry.
        resp.raise_for_status()

    @retry(**_RETRY_KWARGS)
    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.get(path, **kwargs)
        log.debug("zy_client.get", path=path, status=resp.status_code)
        self._check_status(resp, path)
        await self._persist_live_state()
        return resp

    @retry(**_RETRY_KWARGS)
    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Call start() first"
        resp = await self._client.post(path, **kwargs)
        log.debug("zy_client.post", path=path, status=resp.status_code)
        self._check_status(resp, path)
        await self._persist_live_state()
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
        TODO: dedicated endpoint not yet captured. Wishlist payload already carries
        per-item availability; consider extracting from there to avoid a second hop.
        """
        resp = await self._get(
            f"/front/api/products/{product_id}",
            params={"locale": "ru"},
        )
        return resp.json().get("data", {})

    async def add_to_cart(self, sku: str | int, qty: int = 1) -> dict[str, Any]:
        """
        Add item to cart. Captured request:
          POST /front/api/cart/item?locale=ru
          { sku, fiasId, isPlaid, cartBeautiesStore, ... }
        Returns the new cart payload (data.cart.sections[].items[]).
        """
        body: dict[str, Any] = {
            "sku": str(sku),
            "fiasId": self._city_id,
            "isPlaid": True,
            "cartBeautiesStore": True,
        }
        if qty > 1:
            body["qty"] = qty
        resp = await self._post(
            "/front/api/cart/item",
            params={"locale": "ru"},
            json=body,
        )
        return resp.json().get("data", {})

    async def get_checkout_url(self) -> str:
        return f"{self._base_url}/cart"

    async def is_authenticated(self) -> bool:
        # Probe getTicker for favoritesProducts: it 403s for anon clients and
        # 200s for authenticated ones. /user/info/full needs a Bearer token GA
        # serves separately from cookies, so it 401s even on a valid session.
        assert self._client, "Call start() first"
        params: dict[str, Any] = {
            "locale": "ru",
            "pageType": "favoritesProducts",
            "moduleType": "customer",
        }
        if self._city_id:
            params["cityId"] = self._city_id
        try:
            resp = await self._client.get("/front/api/ticker/getTicker", params=params)
        except Exception:
            return False
        if resp.status_code == 200:
            # Capture rotated anti-bot cookies from this probe response.
            await self._persist_live_state()
            return True
        return False
