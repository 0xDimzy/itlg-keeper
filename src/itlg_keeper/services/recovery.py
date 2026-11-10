"""Burn recovery.

Some mining cycles burn part of the balance; the app lets the owner recover it
transaction by transaction. Unlike the original implementation - which ran
recovery only as a side effect of a successful claim - this service is a
first-class citizen with its own cadence, its own ledger entries, and a cap on
how many burns to touch per pass.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..errors import ApiError, TransientError
from ..http import ApiClient
from ..ledger import Ledger
from ..util import as_bool, as_float, pick_amount
from .account import AccountService

log = logging.getLogger("itlg_keeper.recovery")

MAX_BURNS_PER_PASS = 25


@dataclass(slots=True)
class Burn:
    transaction_id: str
    amount: float | None
    recoverable: bool


@dataclass(slots=True)
class RecoverySnapshot:
    can_recover: bool = False
    total: float = 0.0
    burns: list[Burn] = field(default_factory=list)

    @property
    def claimable(self) -> list[Burn]:
        return [b for b in self.burns if b.recoverable]


@dataclass(slots=True)
class RecoveryOutcome:
    claimed_count: int = 0
    amount: float | None = None
    attempted: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def claimed(self) -> bool:
        return self.claimed_count > 0

    @property
    def message(self) -> str:
        if not self.claimed:
            return "tidak ada burn yang bisa dipulihkan"
        amount = f"{self.amount:,.4f}" if self.amount is not None else "?"
        return f"pemulihan: {self.claimed_count} transaksi, +{amount} ITLG"


class RecoveryService:
    def __init__(
        self,
        client: ApiClient,
        account: AccountService,
        ledger: Ledger,
        *,
        sleep=asyncio.sleep,
        settle_seconds: float = 15.0,
    ) -> None:
        self.client = client
        self.account = account
        self.ledger = ledger
        self._sleep = sleep
        self._settle_seconds = settle_seconds

    async def snapshot(self) -> RecoverySnapshot:
        total = 0.0
        can_recover = False
        try:
            envelope = await self.client.request("GET", "/recovery/total-recoverable")
            data = envelope.dict_data()
            total = as_float(data.get("totalRecoverable"), 0.0) or 0.0
            can_recover = as_bool(data.get("canRecover"))
        except ApiError as exc:
            log.debug("total-recoverable tidak tersedia: %s", exc)

        burns: list[Burn] = []
        try:
            listing = await self.client.request("GET", "/recovery/my")
            payload = listing.dict_data()
            rows = payload.get("data")
            if not isinstance(rows, list):
                rows = payload.get("items") if isinstance(payload.get("items"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                transaction_id = row.get("transactionId") or row.get("id")
                if transaction_id is None:
                    continue
                burns.append(
                    Burn(
                        transaction_id=str(transaction_id),
                        amount=as_float(row.get("amount")),
                        recoverable=as_bool(row.get("isRecoverable"))
                        or (as_float(row.get("totalRecoverable"), 0.0) or 0.0) > 0,
                    )
                )
        except ApiError as exc:
            log.debug("daftar burn tidak tersedia: %s", exc)

        if not can_recover and burns:
            can_recover = any(b.recoverable for b in burns)
        if not total and burns:
            total = sum(b.amount or 0.0 for b in burns if b.recoverable)
        return RecoverySnapshot(can_recover=can_recover, total=total, burns=burns)

    async def claim_all(self, *, max_burns: int = MAX_BURNS_PER_PASS) -> RecoveryOutcome:
        snapshot = await self.snapshot()
        pending = snapshot.claimable[:max_burns]
        if not pending:
            return RecoveryOutcome()

        balance_before = await self.account.balance()
        outcome = RecoveryOutcome()
        recovered = 0.0
        for burn in pending:
            outcome.attempted += 1
            try:
                envelope = await self.client.request(
                    "POST",
                    "/recovery/claim",
                    body={"transactionId": burn.transaction_id},
                    retry_transient=False,
                )
            except ApiError as exc:
                if isinstance(exc, TransientError):
                    outcome.failures.append(f"{burn.transaction_id}: {exc.message}")
                    log.warning("pemulihan %s gagal: %s", burn.transaction_id, exc)
                    await self._sleep(3)
                    continue
                outcome.failures.append(f"{burn.transaction_id}: {exc.message}")
                log.info("pemulihan %s ditolak: %s", burn.transaction_id, exc.message)
                continue

            reported = pick_amount(envelope.data)
            credited = reported or burn.amount
            recovered += credited or 0.0
            outcome.claimed_count += 1
            self.ledger.record_claim(
                kind="recovery",
                status="claimed",
                amount=credited,
                source="response" if reported is not None else "listed-amount",
                message=str(envelope.message or "ok"),
                payload={"transaction_id": burn.transaction_id},
            )
            await self._sleep(1.5)

        if outcome.claimed_count:
            balance_after = await self._settle(balance_before)
            outcome.amount = recovered if recovered else _delta(balance_before, balance_after)
            self.ledger.event(
                "info",
                "recovery",
                f"pemulihan {outcome.claimed_count} burn: +{outcome.amount if outcome.amount is not None else '?'}",
            )
        elif outcome.failures:
            self.ledger.event(
                "warning",
                "recovery",
                f"{len(outcome.failures)} pemulihan gagal: {outcome.failures[0]}",
            )
        return outcome

    async def _settle(self, before: float | None) -> float | None:
        deadline = time.time() + self._settle_seconds
        latest = before
        while time.time() < deadline:
            await self._sleep(2.0)
            latest = await self.account.balance()
            if latest is None:
                continue
            if before is None or latest != before:
                return latest
        return latest


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    delta = after - before
    return delta if delta > 0 else None


__all__ = ["Burn", "RecoveryOutcome", "RecoveryService", "RecoverySnapshot"]
