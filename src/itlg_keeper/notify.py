"""Telegram notifications and command handling.

Deliberately dependency-free: the Bot API is a JSON endpoint, and pulling in a
framework for nine commands would add far more surface than it removes.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import TelegramSettings
from .constants import TELEGRAM_API

log = logging.getLogger("itlg_keeper.notify")

MAX_MESSAGE = 3800  # Telegram hard-limits at 4096; leave room for markup.


class Notifier:
    def __init__(self, telegram: TelegramSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.telegram = telegram
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return self.telegram.active

    async def send(self, text: str, *, silent: bool = False) -> bool:
        if not self.enabled:
            log.debug("telegram tidak aktif; pesan dibuang: %s", text[:80])
            return False
        assert self.telegram.token is not None and self.telegram.chat_id is not None
        url = f"{TELEGRAM_API}/bot{self.telegram.token.get_secret_value()}/sendMessage"
        payload = {
            "chat_id": self.telegram.chat_id,
            "text": _trim(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if silent:
            payload["disable_notification"] = True
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("notifikasi telegram gagal: %s", exc)
            return False
        if response.status_code != 200:
            log.warning("telegram menolak pesan: %s %s", response.status_code, response.text[:200])
            return False
        return True


Handler = Callable[[str, str], Awaitable[str | None]]


class CommandBot:
    """Long-polling owner-only bot.

    Only the configured chat id is served. Commands are plain functions that
    return HTML; the bot handles transport, offsets and error reporting.
    """

    def __init__(
        self,
        telegram: TelegramSettings,
        dispatch: Handler,
        *,
        poll_timeout: int = 25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.telegram = telegram
        self.dispatch = dispatch
        self.poll_timeout = poll_timeout
        self._client = httpx.AsyncClient(timeout=poll_timeout + 15, transport=transport)
        self._offset: int | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return self.telegram.active

    def _url(self, method: str) -> str:
        assert self.telegram.token is not None
        return f"{TELEGRAM_API}/bot{self.telegram.token.get_secret_value()}/{method}"

    async def verify(self) -> str | None:
        """Return the bot username when the token works."""
        if not self.enabled:
            return None
        response = await self._client.get(self._url("getMe"))
        payload = response.json() if response.content else {}
        if not payload.get("ok"):
            raise RuntimeError(f"token bot ditolak Telegram: {payload.get('description')}")
        return str(payload["result"].get("username"))

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            log.info("perintah Telegram dimatikan")
            return
        errors = 0
        while not stop.is_set():
            try:
                updated = await self._poll_once(stop)
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += 1
                delay = min(5 * errors, 60)
                log.warning("polling telegram gagal (%s); ulang dalam %ds", exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    continue
                return
            if not updated:
                await asyncio.sleep(0.5)

    async def _poll_once(self, stop: asyncio.Event) -> bool:
        response = await self._client.get(
            self._url("getUpdates"),
            params={
                "timeout": self.poll_timeout,
                "allowed_updates": '["message"]',
                **({"offset": self._offset} if self._offset is not None else {}),
            },
            timeout=self.poll_timeout + 10,
        )
        payload = response.json() if response.content else {}
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or "getUpdates gagal")
        handled = False
        for update in payload.get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            await self._handle(update)
            handled = True
        return handled

    async def _handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = str(message.get("text") or "").strip()
        if not text:
            return
        owner = str(self.telegram.chat_id)
        if chat_id != owner:
            log.info("pesan dari chat %s diabaikan (bukan owner)", chat_id)
            return
        reply = await self.dispatch(text, chat_id)
        if reply:
            await self._send(reply)

    async def _send(self, text: str) -> None:
        try:
            await self._client.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self.telegram.chat_id,
                    "text": _trim(text),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("gagal mengirim balasan telegram: %s", exc)


def esc(value: Any) -> str:
    return html.escape(str(value))


def _trim(text: str) -> str:
    if len(text) <= MAX_MESSAGE:
        return text
    return text[: MAX_MESSAGE - 3] + "..."
