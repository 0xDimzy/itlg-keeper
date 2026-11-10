"""24-hour group mining claim.

The list endpoint returns every group the account belongs to plus a single
24-hour timer. One claim call settles all groups at once, so the keeper picks
the first claimable group as the trigger and records the aggregate reward.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..constants import GROUP_INTERVAL
from ..errors import ApiError, TransientError
from ..http import ApiClient
from ..ledger import Ledger
from ..util import as_bool, as_float, pick_amount, to_epoch_seconds
from .account import AccountService
from .mining import ClaimOutcome

log = logging.getLogger("itlg_keeper.groups")


@dataclass(slots=True)
class GroupSnapshot:
    groups: list[dict[str, Any]] = field(default_factory=list)
    total_reward: float = 0.0
    claimable_ids: list[str] = field(default_factory=list)
    due_at: float | None = None
    claimed_today: bool = False

    @property
    def can_claim(self) -> bool:
        return bool(self.claimable_ids) and not self.claimed_today


class GroupService:
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

    async def snapshot(self) -> GroupSnapshot:
        envelope = await self.client.request(
            "POST", "/group-mining/get-list-group-mining", body={}, retry_transient=True
        )
        data = envelope.dict_data()
        raw_groups = data.get("groups")
        groups = [g for g in raw_groups if isinstance(g, dict)] if isinstance(raw_groups, list) else []

        # Sum every group, not just the ones seen before the first claimable one.
        total = 0.0
        claimable: list[str] = []
        for group in groups:
            total += as_float(group.get("totalReward"), 0.0) or 0.0
            if as_bool(group.get("canClaim")):
                group_id = group.get("groupId")
                if group_id is not None:
                    claimable.append(str(group_id))
        return GroupSnapshot(
            groups=groups,
            total_reward=total,
            claimable_ids=claimable,
            due_at=to_epoch_seconds(data.get("nextTimeClaim")),
            claimed_today=as_bool(data.get("requesterHasClaimedToday")),
        )

    async def claim(self, *, force: bool = False, dry_run: bool | None = None) -> ClaimOutcome:
        dry_run = self.settings.runtime.dry_run if dry_run is None else dry_run
        snapshot = await self.snapshot()
        if not snapshot.can_claim and not force:
            return ClaimOutcome(
                claimed=False,
                reason="belum waktunya" if snapshot.claimed_today else "tidak ada grup claimable",
                due_at=snapshot.due_at,
                source="local",
            )
        if not snapshot.claimable_ids:
            return ClaimOutcome(
                claimed=False,
                reason="tidak ada grup claimable",
                due_at=snapshot.due_at,
                source="local",
            )

        group_id = snapshot.claimable_ids[0]
        balance_before = await self.account.balance()
        if dry_run:
            self.ledger.event("info", "group", f"dry-run: klaim grup {group_id} dilewati")
            return ClaimOutcome(
                claimed=False,
                reason="dry-run",
                balance_before=balance_before,
                due_at=snapshot.due_at,
                source="dry-run",
            )

        try:
            envelope = await self.client.request(
                "POST",
                "/group-mining/claim-group-mining",
                body={"groupId": group_id},
                retry_transient=False,
            )
        except ApiError as exc:
            if "ALREADY_CLAIMED" in f"{exc.message}".upper().replace(" ", "_"):
                self.ledger.record_claim(
                    kind="group", status="already", message=str(exc.message), source="api"
                )
                return ClaimOutcome(
                    claimed=False,
                    reason="sudah diklaim (ALREADY_CLAIMED)",
                    due_at=snapshot.due_at or time.time() + GROUP_INTERVAL,
                    source="api",
                )
            if isinstance(exc, TransientError):
                log.warning("klaim grup gagal sementara: %s", exc)
            raise

        balance_after = await self._settle(balance_before)
        amount = pick_amount(envelope.data)
        source = "response" if amount is not None else "balance-delta"
        if amount is None and balance_before is not None and balance_after is not None:
            delta = balance_after - balance_before
            amount = delta if delta > 0 else None

        due_at = await self._next_due(snapshot.due_at)
        self.ledger.record_claim(
            kind="group",
            status="claimed",
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            source=source,
            message=str(envelope.message or "ok"),
            payload={"group_id": group_id, "groups": len(snapshot.groups)},
        )
        self.ledger.event("info", "group", f"klaim grup: +{amount if amount is not None else '?'}")
        return ClaimOutcome(
            claimed=True,
            reason="ok",
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            due_at=due_at,
            source=source,
            payload={"group_id": group_id},
        )

    async def _next_due(self, fallback: float | None) -> float:
        try:
            snapshot = await self.snapshot()
        except ApiError:
            return fallback or (time.time() + GROUP_INTERVAL)
        return snapshot.due_at or (time.time() + GROUP_INTERVAL)

    async def _settle(self, before: float | None) -> float | None:
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
