"""Scheduler and ledger: deadlines, pause switch, worker reporting."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from itlg_keeper.ledger import Ledger
from itlg_keeper.scheduler import MiningWorker, Scheduler, TickResult
from itlg_keeper.services.mining import ClaimOutcome, Gate


class StubNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, *, silent: bool = False) -> bool:
        self.sent.append(text)
        return True


def _ctx(ledger, settings, mining=None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        ledger=ledger,
        mining=mining,
        notifier=StubNotifier(),
    )


class StubMining:
    def __init__(self, outcome: ClaimOutcome | None = None, gate: Gate | None = None) -> None:
        self.outcome = outcome
        self._gate = gate

    async def gate(self) -> Gate:
        assert self._gate is not None
        return self._gate

    async def claim(self, **_: object) -> ClaimOutcome:
        assert self.outcome is not None
        return self.outcome


def test_describe_due_reads_hint(ledger, settings):
    ledger.put("due.mining", time.time() + 120)
    worker = MiningWorker(_ctx(ledger, settings))
    due, text = worker.describe_due()
    assert due is not None
    assert "2m" in text


async def test_next_due_prefers_future_hint(ledger, settings):
    future = time.time() + 300
    ledger.put("due.mining", future)
    worker = MiningWorker(_ctx(ledger, settings))
    assert await worker.next_due() == pytest.approx(future, abs=1)


async def test_next_due_asks_api_when_hint_is_stale(ledger, settings):
    ledger.put("due.mining", time.time() - 10)
    due = time.time() + 500
    mining = StubMining(gate=Gate(claimable=False, due_at=due))
    worker = MiningWorker(_ctx(ledger, settings, mining=mining))
    assert await worker.next_due() == pytest.approx(due, abs=1)
    assert ledger.get("due.mining") == pytest.approx(due, abs=1)


async def test_tick_reports_claimed_amount(ledger, settings):
    mining = StubMining(
        outcome=ClaimOutcome(
            claimed=True,
            reason="ok",
            amount=12.3456,
            balance_after=200.0,
            due_at=time.time() + 14400,
        )
    )
    ctx = _ctx(ledger, settings, mining=mining)
    worker = MiningWorker(ctx)
    result = await worker.tick()
    assert isinstance(result, TickResult)
    assert result.message is not None
    assert "12.3456" in result.message
    assert result.due_at is not None


async def test_worker_reports_pause_switch(ledger, settings):
    scheduler = Scheduler(_ctx(ledger, settings))
    assert scheduler.is_paused is False
    scheduler.pause()
    assert scheduler.is_paused is True
    scheduler.resume()
    assert scheduler.is_paused is False


def test_scheduler_status_lines_cover_all_features(ledger, settings):
    scheduler = Scheduler(_ctx(ledger, settings))
    labels = [label for label, _ in scheduler.status_lines()]
    assert labels == ["Mining 4 jam", "Group mining 24 jam", "Pemulihan burn"]


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def test_records_and_totals(tmp_path):
    with Ledger(tmp_path / "k.db") as db:
        db.record_claim(kind="mining", status="claimed", amount=10.0)
        db.record_claim(kind="mining", status="claimed", amount=5.5)
        db.record_claim(kind="group", status="already")

        totals = db.claim_totals(since=time.time() - 60)
        assert totals["count"] == 2
        assert totals["amount"] == pytest.approx(15.5)
        assert totals["by_kind"]["mining"]["count"] == 2

        assert db.last_claim_at("mining") is not None
        assert db.recent_claims(limit=10)[0].kind == "group"


def test_kv_roundtrip(tmp_path):
    with Ledger(tmp_path / "k.db") as db:
        db.put("due.mining", 1234.5)
        assert db.get("due.mining") == 1234.5
        assert db.get("missing", "fallback") == "fallback"
        db.delete("due.mining")
        assert db.get("due.mining") is None


def test_prune_removes_old_rows(tmp_path):
    with Ledger(tmp_path / "k.db") as db:
        db.record_claim(kind="mining", status="claimed", amount=1.0)
        db._db.execute("UPDATE claims SET created_at = ?", (time.time() - 40 * 86400,))
        db._db.commit()
        assert db.prune(keep_days=30) == 1
        assert db.recent_claims() == []


def test_token_roundtrip(tmp_path):
    with Ledger(tmp_path / "k.db") as db:
        assert db.load_token() is None
        db.save_token("a", "r", 123.0)
        stored = db.load_token()
        assert stored is not None and stored.access == "a" and stored.refresh == "r"
        db.clear_token()
        assert db.load_token() is None
