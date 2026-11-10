"""Shared fixtures: a fake Interlink API and a wired-up object graph.

No test touches the network. ``httpx.MockTransport`` plays the server, which
also means tests can assert on the exact bytes and headers the keeper sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import httpx
import pytest

from itlg_keeper.auth.tokens import TokenManager
from itlg_keeper.config import (
    AccountSettings,
    DeviceIdentity,
    RuntimeSettings,
    Settings,
    TelegramSettings,
)
from itlg_keeper.http import ApiClient
from itlg_keeper.ledger import Ledger
from itlg_keeper.services.account import AccountService


class FakeApi:
    """A scripted stand-in for prod.interlinklabs.ai.

    Routes are queues: a pushed response is consumed once, and the last one
    repeats forever. That makes multi-step flows (challenge, retry, settle)
    readable in the test body.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # -- scripting ---------------------------------------------------------
    def push(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        http: int = 200,
        status_code: int | str = 200,
        message: str = "ok",
        raw_text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeApi:
        spec: dict[str, Any] = {
            "http": http,
            "status_code": status_code,
            "message": message,
            "data": data,
            "raw_text": raw_text,
            "headers": headers or {},
        }
        self._routes.setdefault((method.upper(), path), []).append(spec)
        return self

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == method.upper() and r.url.path.replace("/api/v1", "") == path
        ]

    # -- transport ---------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path.replace("/api/v1", ""))
        queue = self._routes.get(key)
        if not queue:
            return httpx.Response(
                404, json={"statusCode": 404, "message": f"no route {key}", "data": None}
            )
        spec = queue[0] if len(queue) == 1 else queue.pop(0)
        if spec["raw_text"] is not None:
            return httpx.Response(
                spec["http"],
                text=spec["raw_text"],
                headers={"content-type": "text/html", **spec["headers"]},
            )
        return httpx.Response(
            spec["http"],
            json={
                "statusCode": spec["status_code"],
                "message": spec["message"],
                "data": spec["data"],
            },
            headers=spec["headers"],
        )


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        account=AccountSettings(
            login_id="12345678",
            passcode="123456",
            email="owner@example.com",
            imap_password="app-password",
        ),
        runtime=RuntimeSettings(
            data_dir=tmp_path,
            jitter_min=0,
            jitter_max=0,
            settle_seconds=0.05,
            max_retries=2,
        ),
        telegram=TelegramSettings(),
        device=DeviceIdentity(
            device_id="0123456789abcdef", brand="XiaoMi", model="M2101K6G"
        ),
    )


@pytest.fixture
def ledger(settings) -> Ledger:
    database = Ledger(settings.ledger_path)
    database.save_token("opaque-access-token", "opaque-refresh-token", None)
    yield database
    database.close()


@pytest.fixture
def tokens(ledger) -> TokenManager:
    return TokenManager(ledger)


@pytest.fixture
def client(settings, tokens, fake) -> ApiClient:
    api = ApiClient(settings, tokens, transport=fake.transport())

    async def _no_backoff(attempt: int) -> None:  # keep the suite fast
        return None

    api._sleep_backoff = _no_backoff  # type: ignore[method-assign]
    return api


@pytest.fixture
def account(client) -> AccountService:
    return AccountService(client)


def make_jwt(exp: float, *, subject: str = "123") -> str:
    """Build an unsigned JWT-shaped token whose payload carries ``exp``."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": subject}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature"


def content_hash(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


def profile_payload(balance: float, *, last_claim_at: float | None = None) -> dict[str, Any]:
    return {
        "userInfo": {"username": "tester"},
        "token": {
            "interlinkGoldTokenAmount": balance,
            "lastClaimTime": int((last_claim_at or time.time()) * 1000),
            "dailyMiningRate": 12.5,
            "groupMiningRate": 45.0,
            "directReferralsHashRate": 1.5,
            "indirectReferralHashRate": 0.5,
            "totalReferral": 7,
            "itlgRecoverable": 0,
        },
    }
