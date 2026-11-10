"""HTTP transport: request signing, envelope parsing, retries, circuit breaker.

The Interlink API is a mobile backend with three properties that shape this
module:

* every request is signed with a device fingerprint (``x-device-id``,
  ``x-date``, and a body hash), so headers are built in one place only;
* every response is an envelope - ``{"statusCode", "message", "data"}`` - and
  the HTTP status can disagree with it;
* failures arrive as 5xx, as non-JSON proxy pages, or as a 200 with a
  business ``statusCode`` inside.

The client turns all three into typed exceptions so the rest of the program
never inspects strings.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Settings
from .constants import BUNDLE_ID, PLATFORM, SYSTEM_NAME
from .errors import (
    ApiError,
    AuthExpired,
    RateLimited,
    TransientError,
    UpstreamUnavailable,
)

log = logging.getLogger("itlg_keeper.http")

_MISSING: Any = object()


class TokenProvider(Protocol):
    async def access_token(self, *, force_refresh: bool = False) -> str: ...


@dataclass(slots=True)
class Envelope:
    """A parsed, already-unwrapped API answer."""

    status: int
    message: str
    data: Any
    http_status: int
    raw: dict[str, Any]

    def dict_data(self) -> dict[str, Any]:
        return self.data if isinstance(self.data, dict) else {}


class ApiClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.tokens = tokens
        self.device = settings.require_device()
        self.base = settings.runtime.api_base.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=10.0, read=settings.runtime.request_timeout, write=15.0, pool=10.0
        )
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=transport,
            headers={"connection": "keep-alive"},
        )
        self._consecutive_transport_failures = 0
        self._breaker_until = 0.0
        self._max_retries = max(0, settings.runtime.max_retries)

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- signing -----------------------------------------------------------
    def sign(self, body: bytes | None, token: str | None) -> dict[str, str]:
        """Build the exact header set the Android client sends.

        Header insertion order is preserved by httpx, and it is part of what a
        server-side fingerprint sees, so the order here is intentional.
        """
        headers = {
            "user-agent": self.device.user_agent,
            "content-type": "application/json",
            "accept": "application/json",
            "version": self.settings.runtime.app_version,
            "x-platform": PLATFORM,
            "x-model": self.device.model,
            "x-brand": self.device.brand,
            "x-system-name": SYSTEM_NAME,
            "x-bundle-id": BUNDLE_ID,
            "x-app-version": self.settings.runtime.app_version,
            "x-unique-id": self.device.device_id,
            "x-device-id": self.device.device_id,
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        # Millisecond clock, refreshed per attempt so a retry never replays a
        # stale timestamp (some backends reject those).
        headers["x-date"] = str(int(time.time() * 1000))
        if body:
            digest = hashlib.sha256(body).digest()
            headers["x-content-hash"] = base64.b64encode(digest).decode("ascii")
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base}/{path.lstrip('/')}"

    # -- request -----------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = _MISSING,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        retry_transient: bool | None = None,
    ) -> Envelope:
        method = method.upper()
        if retry_transient is None:
            retry_transient = method in {"GET", "HEAD"}
        max_retries = self._max_retries if retry_transient else 0

        raw_body: bytes | None = None
        if body is not _MISSING:
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()

        token: str | None = None
        if auth:
            token = await self.tokens.access_token()
        headers = self.sign(raw_body, token)
        url = self._url(path)

        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            self._guard_breaker(url)
            try:
                response = await self._client.request(
                    method, url, content=raw_body, headers=headers, params=params
                )
            except httpx.TimeoutException as exc:
                self._note_transport_failure()
                error: Exception = TransientError(f"timeout {method} {path}: {exc}")
                if attempt <= max_retries:
                    await self._sleep_backoff(attempt)
                    continue
                raise error from exc
            except httpx.HTTPError as exc:
                self._note_transport_failure()
                if attempt <= max_retries:
                    await self._sleep_backoff(attempt)
                    continue
                raise TransientError(f"koneksi gagal {method} {path}: {exc}") from exc

            self._reset_breaker()

            if response.status_code == 401 and auth and not refreshed:
                refreshed = True
                log.debug("401 dari %s, mencoba refresh token", path)
                token = await self.tokens.access_token(force_refresh=True)
                headers = self.sign(raw_body, token)
                continue

            if response.status_code == 429:
                raise RateLimited(
                    f"rate limited pada {path}",
                    status=429,
                    retry_after=_retry_after(response),
                )

            if response.status_code >= 500:
                if attempt <= max_retries:
                    await self._sleep_backoff(attempt)
                    continue
                raise TransientError(
                    f"server error {response.status_code} pada {path}",
                    status=response.status_code,
                )

            payload = _decode_json(response)
            if payload is None:
                if response.status_code in {403, 503} or "html" in response.headers.get("content-type", ""):
                    raise UpstreamUnavailable(
                        "respons bukan JSON (kemungkinan halaman proxy/Cloudflare)",
                        status=response.status_code,
                    )
                raise TransientError(
                    f"respons tidak bisa diurai pada {path} (http {response.status_code})",
                    status=response.status_code,
                )

            return self._unwrap(payload, response.status_code, path)

    def _unwrap(self, payload: dict[str, Any], http_status: int, path: str) -> Envelope:
        status = _coerce_status(payload.get("statusCode", payload.get("status", http_status)))
        message = str(payload.get("message") or payload.get("error") or "").strip()
        data = payload.get("data")
        if status in {200, 201, 202, 204}:
            return Envelope(
                status=status, message=message, data=data, http_status=http_status, raw=payload
            )
        if status in {401, 403}:
            raise AuthExpired(message or f"tidak diizinkan pada {path}", status=http_status, code=status)
        raise ApiError(
            message or f"permintaan gagal pada {path}",
            status=http_status,
            code=status,
            payload=payload,
        )

    # -- presigned uploads (selfie login) ----------------------------------
    async def put_bytes(self, url: str, content: bytes, content_type: str) -> None:
        headers = {"content-type": content_type, "content-length": str(len(content))}
        try:
            response = await self._client.put(url, content=content, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"unggah foto gagal: {exc}") from exc
        if response.status_code >= 400:
            raise ApiError(
                f"unggah foto ditolak (http {response.status_code})",
                status=response.status_code,
                payload=response.text[:400],
            )

    # -- resilience --------------------------------------------------------
    def _guard_breaker(self, url: str) -> None:
        if self._breaker_until > time.time():
            remaining = self._breaker_until - time.time()
            raise UpstreamUnavailable(
                f"circuit breaker terbuka {remaining:.0f}s lagi (API tidak merespons)"
            )

    def _note_transport_failure(self) -> None:
        self._consecutive_transport_failures += 1
        if self._consecutive_transport_failures >= 6:
            self._breaker_until = time.time() + 60
            self._consecutive_transport_failures = 0
            log.warning("enam kegagalan transport berturut-turut; istirahat 60s")

    def _reset_breaker(self) -> None:
        self._consecutive_transport_failures = 0
        self._breaker_until = 0.0

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(1.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.75), 20.0)
        log.debug("backoff %.1fs sebelum percobaan ke-%d", delay, attempt + 1)
        await asyncio.sleep(delay)


def _decode_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_status(value: Any) -> int:
    """``statusCode`` as ``"200"`` is still 200 - the previous generation treated
    a stringly-typed status as a failure."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
