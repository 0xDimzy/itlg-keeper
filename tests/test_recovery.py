"""Recovery: listing burns, claiming each, capping per pass."""

from __future__ import annotations

import pytest

from conftest import profile_payload
from itlg_keeper.services.recovery import RecoveryService


async def _noop(_delay: float) -> None:
    return None


@pytest.fixture
def service(client, account, ledger) -> RecoveryService:
    return RecoveryService(client, account, ledger, sleep=_noop, settle_seconds=0.05)


def _burns(*rows: tuple[str, float, bool]) -> dict:
    return {
        "data": [
            {"transactionId": tid, "amount": amount, "isRecoverable": flag}
            for tid, amount, flag in rows
        ]
    }


async def test_snapshot_maps_list(service, fake):
    fake.push("GET", "/recovery/total-recoverable", {"canRecover": True, "totalRecoverable": 6.5})
    fake.push("GET", "/recovery/my", _burns(("t1", 4.0, True), ("t2", 2.5, False)))

    snapshot = await service.snapshot()

    assert snapshot.can_recover is True
    assert snapshot.total == 6.5
    assert len(snapshot.burns) == 2
    assert [b.transaction_id for b in snapshot.claimable] == ["t1"]


async def test_claims_only_recoverable_burns(service, fake, ledger):
    fake.push("GET", "/recovery/total-recoverable", {"canRecover": True, "totalRecoverable": 4.0})
    fake.push("GET", "/recovery/my", _burns(("t1", 4.0, True), ("t2", 2.0, False)))
    fake.push("GET", "/auth/current-user-full", profile_payload(10.0))
    fake.push("POST", "/recovery/claim", {"amount": 4.0})
    fake.push("GET", "/auth/current-user-full", profile_payload(14.0))

    outcome = await service.claim_all()

    assert outcome.claimed is True
    assert outcome.claimed_count == 1
    assert outcome.amount == pytest.approx(4.0)
    assert len(fake.calls("POST", "/recovery/claim")) == 1
    assert ledger.recent_claims(kind="recovery")[0].amount == pytest.approx(4.0)


async def test_cap_per_pass(service, fake):
    rows = tuple((f"t{i}", 1.0, True) for i in range(30))
    fake.push("GET", "/recovery/total-recoverable", {"canRecover": True, "totalRecoverable": 30.0})
    fake.push("GET", "/recovery/my", _burns(*rows))
    fake.push("GET", "/auth/current-user-full", profile_payload(0.0))
    fake.push("POST", "/recovery/claim", {"amount": 1.0})
    fake.push("GET", "/auth/current-user-full", profile_payload(25.0))

    outcome = await service.claim_all(max_burns=5)

    assert outcome.attempted == 5
    assert len(fake.calls("POST", "/recovery/claim")) == 5


async def test_nothing_to_do(service, fake):
    fake.push("GET", "/recovery/total-recoverable", {"canRecover": False, "totalRecoverable": 0})
    fake.push("GET", "/recovery/my", {"data": []})

    outcome = await service.claim_all()

    assert outcome.claimed is False
    assert outcome.attempted == 0


async def test_uses_listed_amount_when_response_is_empty(service, fake, ledger):
    fake.push("GET", "/recovery/total-recoverable", {"canRecover": True, "totalRecoverable": 2.5})
    fake.push("GET", "/recovery/my", _burns(("t9", 2.5, True)))
    fake.push("GET", "/auth/current-user-full", profile_payload(1.0))
    fake.push("POST", "/recovery/claim", {})
    fake.push("GET", "/auth/current-user-full", profile_payload(1.0))

    outcome = await service.claim_all()

    assert outcome.amount == pytest.approx(2.5)
    assert ledger.recent_claims(kind="recovery")[0].source == "listed-amount"
