"""Transport behaviour: signing, envelope parsing, retries, breaker."""

from __future__ import annotations

import base64
import hashlib

import pytest

from itlg_keeper.auth.tokens import TokenPair
from itlg_keeper.errors import AuthExpired, RateLimited, TransientError, UpstreamUnavailable

from conftest import content_hash, make_jwt


async def test_signed_headers_match_the_android_client(client, fake, settings):
    fake.push("POST", "/auth/check-passcode", {"ok": True})
    await client.request(
        "POST", "/auth/check-passcode", body={"loginId": "1"}, params={"v": "2"}, auth=False
    )
    request = fake.requests[-1]
    body = request.content

    assert request.headers["x-platform"] == "android"
    assert request.headers["x-bundle-id"] == "org.ai.interlinklabs.interlinkId"
    assert request.headers["version"] == settings.runtime.app_version
    assert request.headers["x-app-version"] == settings.runtime.app_version
    assert request.headers["x-device-id"] == "0123456789abcdef"
    assert request.headers["x-unique-id"] == "0123456789abcdef"
    assert request.headers["x-model"] == "M2101K6G"
    assert request.headers["x-brand"] == "XiaoMi"
    assert request.headers["x-date"].isdigit() and len(request.headers["x-date"]) == 13
    assert request.headers["x-content-hash"] == content_hash(body)
    assert "authorization" not in request.headers


async def test_content_hash_is_sha256_of_exact_bytes(client, fake):
    fake.push("POST", "/x", {})
    await client.request("POST", "/x", body={"a": 1, "b": "dua"}, auth=False)
    sent = fake.requests[-1].content
    expected = base64.b64encode(hashlib.sha256(sent).digest()).decode()
    assert fake.requests[-1].headers["x-content-hash"] == expected


async def test_get_has_no_content_hash_but_has_date(client, fake):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": False})
    await client.request("GET", "/token/check-is-claimable")
    request = fake.requests[-1]
    assert "x-content-hash" not in request.headers
    assert "x-date" in request.headers
    assert request.headers["authorization"] == "Bearer opaque-access-token"


async def test_stringly_typed_status_is_success(client, fake):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True}, status_code="200")
    envelope = await client.request("GET", "/token/check-is-claimable")
    assert envelope.data == {"isClaimable": True}


async def test_business_error_raises_api_error_with_code(client, fake):
    from itlg_keeper.errors import ApiError

    fake.push("POST", "/token/claim-airdrop", None, status_code=400, message="TOO_EARLY")
    with pytest.raises(ApiError) as caught:
        await client.request("POST", "/token/claim-airdrop", body={})
    assert caught.value.code == 400
    assert "TOO_EARLY" in str(caught.value)


async def test_rate_limit_carries_retry_after(client, fake):
    fake.push("GET", "/token/check-is-claimable", None, http=429, headers={"retry-after": "12"})
    with pytest.raises(RateLimited) as caught:
        await client.request("GET", "/token/check-is-claimable")
    assert caught.value.retry_after == 12.0


async def test_html_body_is_not_mistaken_for_an_answer(client, fake):
    """A 200 with an HTML body (Cloudflare challenge) must not read as 'not due'."""
    fake.push(
        "GET",
        "/token/check-is-claimable",
        http=200,
        raw_text="<html><body>Just a moment...</body></html>",
    )
    with pytest.raises(UpstreamUnavailable):
        await client.request("GET", "/token/check-is-claimable", retry_transient=False)


async def test_503_html_is_retried_as_transient(client, fake):
    fake.push(
        "GET", "/token/check-is-claimable", http=503, raw_text="<html>bad gateway</html>"
    )
    with pytest.raises(TransientError):
        await client.request("GET", "/token/check-is-claimable")


async def test_get_retries_server_errors_then_succeeds(client, fake):
    fake.push("GET", "/token/check-is-claimable", None, http=500)
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": False})
    envelope = await client.request("GET", "/token/check-is-claimable")
    assert envelope.data == {"isClaimable": False}
    assert len(fake.calls("GET", "/token/check-is-claimable")) == 2


async def test_exhausted_retries_surface_transient_error(client, fake):
    client._max_retries = 1
    fake.push("GET", "/token/check-is-claimable", None, http=500)
    with pytest.raises(TransientError):
        await client.request("GET", "/token/check-is-claimable")


async def test_post_is_not_retried_by_default(client, fake):
    fake.push("POST", "/token/claim-airdrop", None, http=503)
    with pytest.raises(TransientError):
        await client.request("POST", "/token/claim-airdrop", body={})
    assert len(fake.calls("POST", "/token/claim-airdrop")) == 1


async def test_401_triggers_one_refresh_and_one_retry(client, fake, tokens):
    calls: list[str] = []

    async def refresher(refresh_token: str) -> TokenPair:
        calls.append(refresh_token)
        return TokenPair(
            access=make_jwt(9_999_999_999), refresh="refreshed", expires_at=9_999_999_999
        )

    tokens.bind(refresher=refresher, login=None)

    fake.push("GET", "/token/check-is-claimable", None, http=401)
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True})

    envelope = await client.request("GET", "/token/check-is-claimable")
    assert envelope.data == {"isClaimable": True}
    assert calls == ["opaque-refresh-token"]
    assert fake.requests[-1].headers["authorization"].startswith("Bearer ey")


async def test_auth_error_when_refresh_also_fails(client, fake, tokens):
    async def refresher(refresh_token: str) -> TokenPair:
        raise AuthExpired("refresh ditolak")

    tokens.bind(refresher=refresher, login=None)
    fake.push("GET", "/token/check-is-claimable", None, http=401, status_code=401)
    fake.push("GET", "/token/check-is-claimable", None, http=401, status_code=401)

    with pytest.raises(AuthExpired):
        await client.request("GET", "/token/check-is-claimable")
