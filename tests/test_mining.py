"""Mining claim flow, including the failure modes that used to loop forever."""

from __future__ import annotations

import time

import pytest

from itlg_keeper.services.account import AccountService
from itlg_keeper.services.mining import MiningService

from conftest import profile_payload


async def _noop(_delay: float) -> None:
    return None


@pytest.fixture
def service(client, account, ledger, settings) -> MiningService:
    return MiningService(client, account, ledger, settings, sleep=_noop)


def _frame(seconds_from_now: float) -> int:
    return int((time.time() + seconds_from_now) * 1000)


async def test_not_claimable_does_not_post(service, fake):
    fake.push(
        "GET",
        "/token/check-is-claimable",
        {"isClaimable": False, "nextFrame": _frame(3600)},
    )
    outcome = await service.claim()
    assert outcome.claimed is False
    assert "belum" in outcome.reason
    assert outcome.due_at is not None and outcome.due_at > time.time()
    assert fake.calls("POST", "/token/claim-airdrop") == []


async def test_successful_claim_records_balance_delta(service, fake, ledger):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True, "nextFrame": _frame(14400)})
    fake.push("GET", "/auth/current-user-full", profile_payload(100.0))
    fake.push("GET", "/auth/current-user-full", profile_payload(105.4))
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 0})
    fake.push("POST", "/token/claim-airdrop", {"ok": True})

    outcome = await service.claim()

    assert outcome.claimed is True
    assert outcome.amount == pytest.approx(5.4)
    assert outcome.source == "balance-delta"
    assert outcome.balance_before == 100.0
    assert outcome.balance_after == 105.4
    records = ledger.recent_claims(kind="mining")
    assert records[0].status == "claimed"
    assert records[0].amount == pytest.approx(5.4)


async def test_reward_from_response_wins_over_delta(service, fake, ledger):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True})
    fake.push("GET", "/auth/current-user-full", profile_payload(10.0))
    fake.push("GET", "/auth/current-user-full", profile_payload(10.2))
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 0})
    fake.push("POST", "/token/claim-airdrop", {"amount": 7.5})

    outcome = await service.claim()

    assert outcome.claimed is True
    assert outcome.amount == 7.5
    assert outcome.source == "response"


async def test_too_early_is_benign_and_resyncs_timer(service, fake, ledger):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True, "nextFrame": _frame(60)})
    fake.push("GET", "/auth/current-user-full", profile_payload(50.0))
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 0})
    fake.push("POST", "/token/claim-airdrop", None, status_code=400, message="TOO_EARLY")

    outcome = await service.claim()

    assert outcome.claimed is False
    assert "TOO_EARLY" in outcome.reason
    assert outcome.due_at is not None
    assert ledger.recent_claims(kind="mining")[0].status == "already"


async def test_dry_run_never_posts(service, fake, ledger):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True})
    fake.push("GET", "/auth/current-user-full", profile_payload(1.0))
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 0})

    outcome = await service.claim(dry_run=True)

    assert outcome.claimed is False
    assert outcome.reason == "dry-run"
    assert fake.calls("POST", "/token/claim-airdrop") == []


async def test_server_hiccup_is_retried_once(service, fake):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True})
    fake.push("GET", "/auth/current-user-full", profile_payload(5.0))
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 0})
    fake.push("POST", "/token/claim-airdrop", None, http=500)
    fake.push("POST", "/token/claim-airdrop", {"ok": True})

    outcome = await service.claim()

    assert outcome.claimed is True
    assert len(fake.calls("POST", "/token/claim-airdrop")) == 2


async def test_gate_tolerates_missing_timer(service, fake):
    fake.push("GET", "/token/check-is-claimable", {"isClaimable": True})
    gate = await service.gate()
    assert gate.claimable is True
    assert gate.due_at is None


async def test_ads_delay_is_capped(service, fake, monkeypatch):
    waits: list[float] = []

    async def capture(delay: float) -> None:
        waits.append(delay)

    service._sleep = capture  # type: ignore[assignment]
    fake.push("GET", "/token/get-random-ads-mining-new", {"timeRetry": 9999})
    await service._watch_ad(None)
    assert waits and waits[0] <= 182
