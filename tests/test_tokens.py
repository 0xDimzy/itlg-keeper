"""Token lifecycle: expiry maths, opaque tokens, refresh serialisation."""

from __future__ import annotations

import asyncio

import pytest

from conftest import make_jwt
from itlg_keeper.auth.tokens import TokenManager, TokenPair, decode_expiry
from itlg_keeper.errors import LoginUnavailable


def test_decode_expiry_reads_jwt_payload():
    token = make_jwt(1_700_000_000)
    assert decode_expiry(token) == 1_700_000_000


def test_decode_expiry_returns_none_for_opaque_tokens():
    assert decode_expiry("not-a-jwt") is None
    assert decode_expiry("a.b") is None


def test_opaque_token_is_not_treated_as_expired(ledger):
    """The classic bug: an opaque token re-triggers login on every call."""
    manager = TokenManager(ledger)
    assert manager.is_valid() is True
    assert manager.snapshot()["present"] is True


def test_expired_jwt_is_invalid(ledger):
    ledger.save_token(make_jwt(1_000_000), "r", 1_000_000)
    manager = TokenManager(ledger)
    assert manager.is_valid() is False


def test_valid_jwt_is_accepted(ledger):
    future = 9_999_999_999
    ledger.save_token(make_jwt(future), "r", future)
    assert TokenManager(ledger).is_valid() is True


async def test_store_persists_and_reloads(ledger):
    manager = TokenManager(ledger)
    await manager.store(TokenPair(access=make_jwt(9_999_999_999), refresh="r2", expires_at=0))
    stored = ledger.load_token()
    assert stored is not None and stored.access.startswith("ey")
    assert stored.refresh == "r2"


async def test_concurrent_callers_refresh_once(ledger):
    ledger.save_token(make_jwt(1_000_000), "refresh-token", 1_000_000)
    manager = TokenManager(ledger)
    calls: list[str] = []

    async def refresher(refresh_token: str) -> TokenPair:
        calls.append(refresh_token)
        await asyncio.sleep(0.02)
        return TokenPair(access=make_jwt(9_999_999_999), refresh="new", expires_at=9_999_999_999)

    manager.bind(refresher=refresher, login=None)
    results = await asyncio.gather(*(manager.access_token() for _ in range(5)))
    assert len(set(results)) == 1
    assert calls == ["refresh-token"]


async def test_refresh_failure_falls_back_to_login(ledger):
    ledger.save_token(make_jwt(1_000_000), "refresh-token", 1_000_000)
    manager = TokenManager(ledger)
    logins = 0

    async def refresher(refresh_token: str) -> TokenPair:
        from itlg_keeper.errors import AuthExpired

        raise AuthExpired("ditolak")

    async def login() -> TokenPair:
        nonlocal logins
        logins += 1
        return TokenPair(access=make_jwt(9_999_999_999), refresh="fresh", expires_at=9_999_999_999)

    manager.bind(refresher=refresher, login=login)
    await manager.access_token()
    assert logins == 1
    assert ledger.load_token().refresh == "fresh"


async def test_no_login_path_raises_helpful_error(ledger):
    ledger.clear_token()
    manager = TokenManager(ledger)
    with pytest.raises(LoginUnavailable):
        await manager.access_token()


def test_clear_removes_token(ledger):
    manager = TokenManager(ledger)
    manager.clear()
    assert manager.snapshot()["present"] is False
    assert ledger.load_token() is None
