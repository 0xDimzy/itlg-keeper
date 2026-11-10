"""Token lifecycle: expiry, refresh, and the fallback to a fresh login.

Two behaviours are worth calling out because they fix real outages in the
original design:

* A token that is *not* a JWT gets a conservative TTL instead of being treated
  as expired on every call (which caused a refresh/login storm every cycle).
* Refresh is serialised behind an ``asyncio.Lock`` with a double-check, so ten
  concurrent callers produce one refresh, not ten.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..constants import OPAQUE_TOKEN_TTL, TOKEN_SKEW
from ..errors import AuthError, AuthExpired, LoginUnavailable
from ..ledger import Ledger

log = logging.getLogger("itlg_keeper.tokens")


@dataclass(slots=True)
class TokenPair:
    access: str
    refresh: str | None
    expires_at: float
    source: str = "login"


def decode_expiry(token: str) -> float | None:
    """Read ``exp`` from a JWT without pulling in a JWT dependency."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


class TokenManager:
    def __init__(
        self,
        ledger: Ledger,
        *,
        clock: Callable[[], float] = time.time,
        skew: float = TOKEN_SKEW,
        opaque_ttl: float = OPAQUE_TOKEN_TTL,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._skew = skew
        self._opaque_ttl = opaque_ttl
        self._lock = asyncio.Lock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at: float = 0.0
        self._loaded = False
        self._refresher: Callable[[str], Awaitable[TokenPair]] | None = None
        self._login: Callable[[], Awaitable[TokenPair]] | None = None

    # -- wiring ------------------------------------------------------------
    def bind(self, *, refresher, login) -> None:
        self._refresher = refresher
        self._login = login

    # -- persistence -------------------------------------------------------
    def _hydrate(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        stored = self._ledger.load_token()
        if stored is None:
            return
        self._access = stored.access
        self._refresh = stored.refresh
        computed = decode_expiry(stored.access) if stored.access else None
        if computed is not None:
            self._expires_at = computed
        else:
            self._expires_at = stored.updated_at + self._opaque_ttl
        log.debug("token dimuat dari database (kedaluwarsa %.0f)", self._expires_at)

    async def store(self, pair: TokenPair) -> None:
        self._access = pair.access
        self._refresh = pair.refresh
        expires = decode_expiry(pair.access)
        self._expires_at = expires if expires is not None else self._clock() + self._opaque_ttl
        self._loaded = True
        await asyncio.to_thread(
            self._ledger.save_token, pair.access, pair.refresh, self._expires_at
        )
        log.info("token disimpan (sumber=%s, berlaku sampai %s)", pair.source, _ts(self._expires_at))

    def clear(self) -> None:
        self._access = None
        self._refresh = None
        self._expires_at = 0.0
        self._loaded = True
        self._ledger.clear_token()

    @property
    def refresh_token(self) -> str | None:
        self._hydrate()
        return self._refresh

    def is_valid(self) -> bool:
        self._hydrate()
        if not self._access:
            return False
        return self._expires_at - self._skew > self._clock()

    def snapshot(self) -> dict[str, Any]:
        self._hydrate()
        return {
            "present": bool(self._access),
            "has_refresh": bool(self._refresh),
            "expires_at": self._expires_at,
            "seconds_left": max(0.0, self._expires_at - self._clock()),
        }

    # -- acquisition -------------------------------------------------------
    async def access_token(self, *, force_refresh: bool = False) -> str:
        self._hydrate()
        if not force_refresh and self._access and self.is_valid():
            return self._access
        async with self._lock:
            # Someone else may have refreshed while we waited.
            self._hydrate()
            if not force_refresh and self._access and self.is_valid():
                return self._access
            return await self._renew_locked(force_refresh=force_refresh)

    async def _renew_locked(self, *, force_refresh: bool) -> str:
        if self._refresh and self._refresher is not None:
            try:
                pair = await self._refresher(self._refresh)
            except AuthExpired:
                log.info("refresh token ditolak; sesi perlu login ulang")
                self._refresh = None
            except AuthError:
                raise
            except Exception as exc:
                log.warning("refresh gagal karena error tak terduga: %s", exc)
            else:
                await self.store(pair)
                return pair.access
        if force_refresh and self._access and self.is_valid():
            # Refresh was unavailable but the token is still inside its window.
            return self._access
        return await self._login_locked()

    async def _login_locked(self) -> str:
        if self._login is None:
            raise LoginUnavailable(
                "sesi habis dan tidak ada jalur login yang dikonfigurasi. "
                "Jalankan `itlg-keeper login` (butuh IMAP app password atau foto selfie)."
            )
        pair = await self._login()
        await self.store(pair)
        return pair.access


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
