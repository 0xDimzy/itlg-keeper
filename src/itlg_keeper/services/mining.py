"""The 4-hour mining claim.

Sequence, in the order the app performs it:

1. ask whether the frame is claimable and when the next one opens;
2. read the balance (so the reward can be measured);
3. ask the "watch an ad" endpoint and wait the delay it returns;
4. POST the claim;
5. poll the balance until it moves, instead of trusting a fixed two-second nap.

Only step 4 changes state, so everything else is safe to repeat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..constants import MINING_INTERVAL
from ..errors import ApiError, TransientError
from ..http import ApiClient
from ..ledger import Ledger
from ..util import as_bool, as_float, pick_amount, to_epoch_seconds
from .account import AccountService

log = logging.getLogger("itlg_keeper.mining")


@dataclass(slots=True)
class Gate:
    claimable: bool
    due_at: float | None = None
    last_claim_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class ClaimOutcome:
    claimed: bool
    reason: str
    amount: float | None = None
    balance_before: float | None = None
    balance_after: float | None = None
    due_at: float | None = None
    source: str = "unknown"
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def message(self) -> str:
        if self.claimed:
            amount = f"{self.amount:,.4f}" if self.amount is not None else "?"
            return f"klaim mining berhasil: +{amount} ITLG"
        return f"klaim mining tidak dilakukan ({self.reason})"


class MiningService:
    def __init__(
        self,
        client: ApiClient,
        account: AccountService,
        ledger: Ledger,
        settings: Settings,
        *,
        sleep=asyncio.sleep,
    ) -> None:
        self.client = client
        self.account = account
        self.ledger = ledger
        self.settings = settings
        self._sleep = sleep

    # -- reads -------------------------------------------------------------
    async def gate(self) -> Gate:
        envelope = await self.client.request("GET", "/token/check-is-claimable")
        data = envelope.dict_data()
        return Gate(
            claimable=as_bool(data.get("isClaimable")),
            due_at=to_epoch_seconds(data.get("nextFrame")),
            last_claim_at=to_epoch_seconds(data.get("lastClaimTime")),
            raw=data,
        )

    # -- writes ------------------------------------------------------------
    async def claim(self, *, force: bool = False, dry_run: bool | None = None) -> ClaimOutcome:
        dry_run = self.settings.runtime.dry_run if dry_run is None else dry_run
        gate = await self.gate()
        if not gate.claimable and not force:
            return ClaimOutcome(
                claimed=False,
                reason="frame belum terbuka",
                due_at=gate.due_at,
                source="local",
                payload=gate.raw,
            )

        balance_before = await self.account.balance()
        await self._watch_ad(gate.last_claim_at)

        if dry_run:
            self.ledger.event("info", "mining", "dry-run: klaim mining dilewati")
            return ClaimOutcome(
                claimed=False,
                reason="dry-run",
                balance_before=balance_before,
                due_at=gate.due_at,
                source="dry-run",
            )

        try:
            envelope = await self._submit()
        except AlreadyClaimedError as exc:
            log.info("server bilang frame ini sudah diklaim; selaraskan timer")
            self.ledger.record_claim(
                kind="mining",
                status="already",
                message="TOO_EARLY",
                source="api",
            )
            return ClaimOutcome(
                claimed=False,
                reason="sudah diklaim (TOO_EARLY)",
                balance_before=balance_before,
                due_at=exc.due_at or await self._next_due(),
                source="api",
            )
        payload = envelope.raw if isinstance(envelope.raw, dict) else {}
        balance_after = await self._settle(balance_before)

        amount = pick_amount(envelope.data)
        source = "response" if amount is not None else "balance-delta"
        if amount is None and balance_before is not None and balance_after is not None:
            delta = balance_after - balance_before
            amount = delta if delta > 0 else None

        due_at = await self._next_due()
        self.ledger.record_claim(
            kind="mining",
            status="claimed",
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            source=source,
            message=str(envelope.message or "ok"),
            payload=payload,
        )
        self.ledger.event(
            "info",
            "mining",
            f"klaim 4h: +{amount if amount is not None else '?'} (sumber {source})",
        )
        return ClaimOutcome(
            claimed=True,
            reason="ok",
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            due_at=due_at,
            source=source,
            payload=payload,
        )

    async def _submit(self):
        """POST the claim, retrying once on a server-side hiccup."""
        last: Exception | None = None
        for attempt in range(1, 3):
            try:
                return await self.client.request(
                    "POST", "/token/claim-airdrop", body={}, retry_transient=False
                )
            except ApiError as exc:
                if _is_too_early(exc):
                    gate = await self.gate()
                    raise AlreadyClaimedError(gate.due_at) from exc
                last = exc
                if isinstance(exc, TransientError) and attempt == 1:
                    log.warning("klaim mining gagal sementara (%s); ulang dalam 10s", exc)
                    await self._sleep(10)
                    continue
                raise
        assert last is not None
        raise last

    async def _watch_ad(self, last_claim_at: float | None) -> None:
        """The app requires an ad view before a claim; mirror that flow."""
        params = {"totalHhp": "1", "lastTimeClaim": str(int((last_claim_at or 0) * 1000))}
        try:
            envelope = await self.client.request(
                "GET", "/token/get-random-ads-mining-new", params=params
            )
        except ApiError as exc:
            log.debug("endpoint iklan menolak (%s); lanjut dengan jeda pendek", exc)
            await self._sleep(3)
            return
        wait = as_float(envelope.dict_data().get("timeRetry"), 10.0) or 10.0
        wait = max(0.0, min(wait, 180.0)) + 2.0
        log.info("menunggu %.0fs sesuai instruksi iklan", wait)
        await self._sleep(wait)

    async def _settle(self, before: float | None) -> float | None:
        """Poll the balance until it changes (or give up quietly)."""
        deadline = time.time() + self.settings.runtime.settle_seconds
        latest = before
        while time.time() < deadline:
            await self._sleep(2.5)
            latest = await self.account.balance()
            if latest is None:
                continue
            if before is None or latest != before:
                return latest
        return latest

    async def _next_due(self) -> float:
        try:
            gate = await self.gate()
        except ApiError:
            return time.time() + MINING_INTERVAL
        return gate.due_at or (time.time() + MINING_INTERVAL)


class AlreadyClaimedError(Exception):
    """Internal signal: the frame was already consumed elsewhere."""

    def __init__(self, due_at: float | None) -> None:
        super().__init__("sudah diklaim")
        self.due_at = due_at


def _is_too_early(exc: ApiError) -> bool:
    message = f"{exc.message}".upper()
    return str(exc.code) == "400" and "TOO_EARLY" in message.replace(" ", "_")


__all__ = ["AlreadyClaimedError", "ClaimOutcome", "Gate", "MiningService"]
