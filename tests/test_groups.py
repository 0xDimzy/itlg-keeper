"""Group mining: the aggregate-pool bug and the claim path."""

from __future__ import annotations

import time

import pytest

from itlg_keeper.services.groups import GroupService

from conftest import profile_payload


async def _noop(_delay: float) -> None:
    return None


@pytest.fixture
def service(client, account, ledger, settings) -> GroupService:
    return GroupService(client, account, ledger, settings, sleep=_noop)


def _list_payload() -> dict:
    return {
        "groups": [
            {"groupId": "g1", "totalReward": 10.0, "canClaim": False},
            {"groupId": "g2", "totalReward": 25.5, "canClaim": True},
            {"groupId": "g3", "totalReward": 9.5, "canClaim": False},
        ],
        "nextTimeClaim": int((time.time() + 86400) * 1000),
        "requesterHasClaimedToday": False,
    }


async def test_pool_sums_every_group(service, fake):
    """Regression: the legacy loop broke at the first claimable group."""
    fake.push("POST", "/group-mining/get-list-group-mining", _list_payload())
    snapshot = await service.snapshot()
    assert snapshot.total_reward == pytest.approx(45.0)
    assert snapshot.claimable_ids == ["g2"]
    assert snapshot.can_claim is True


async def test_claim_uses_first_claimable_group(service, fake, ledger):
    fake.push("POST", "/group-mining/get-list-group-mining", _list_payload())
    fake.push("GET", "/auth/current-user-full", profile_payload(100.0))
    fake.push("GET", "/auth/current-user-full", profile_payload(145.0))
    fake.push("POST", "/group-mining/claim-group-mining", {"ok": True})

    outcome = await service.claim()

    assert outcome.claimed is True
    assert outcome.amount == pytest.approx(45.0)
    body = fake.calls("POST", "/group-mining/claim-group-mining")[0].content
    assert b'"groupId":"g2"' in body
    assert ledger.recent_claims(kind="group")[0].status == "claimed"


async def test_already_claimed_is_benign(service, fake, ledger):
    fake.push("POST", "/group-mining/get-list-group-mining", _list_payload())
    fake.push("GET", "/auth/current-user-full", profile_payload(1.0))
    fake.push(
        "POST",
        "/group-mining/claim-group-mining",
        None,
        status_code=400,
        message="ALREADY_CLAIMED",
    )

    outcome = await service.claim()

    assert outcome.claimed is False
    assert "ALREADY_CLAIMED" in outcome.reason
    assert ledger.recent_claims(kind="group")[0].status == "already"


async def test_no_claimable_group_skips_network_write(service, fake):
    payload = _list_payload()
    for group in payload["groups"]:
        group["canClaim"] = False
    fake.push("POST", "/group-mining/get-list-group-mining", payload)

    outcome = await service.claim()

    assert outcome.claimed is False
    assert fake.calls("POST", "/group-mining/claim-group-mining") == []


async def test_claimed_today_blocks_claim(service, fake):
    payload = _list_payload()
    payload["requesterHasClaimedToday"] = True
    fake.push("POST", "/group-mining/get-list-group-mining", payload)

    outcome = await service.claim()

    assert outcome.claimed is False
    assert fake.calls("POST", "/group-mining/claim-group-mining") == []


async def test_dry_run(service, fake):
    fake.push("POST", "/group-mining/get-list-group-mining", _list_payload())
    fake.push("GET", "/auth/current-user-full", profile_payload(1.0))

    outcome = await service.claim(dry_run=True)

    assert outcome.reason == "dry-run"
    assert fake.calls("POST", "/group-mining/claim-group-mining") == []
