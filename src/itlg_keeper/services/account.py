"""Read models of the account: profile, balances, rates.

Everything here is a pure *read* - one endpoint, one dataclass, no side
effects - so the rest of the program can call it freely for status displays.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..errors import ApiError
from ..http import ApiClient
from ..util import as_float, as_int, to_epoch_seconds

log = logging.getLogger("itlg_keeper.account")


@dataclass(slots=True)
class Profile:
    username: str | None = None
    balance: float | None = None
    last_claim_at: float | None = None
    daily_rate: float | None = None
    group_rate: float | None = None
    direct_hash_rate: float | None = None
    indirect_hash_rate: float | None = None
    referrals: int = 0
    recoverable: float | None = None
    can_recover: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class AccountService:
    def __init__(self, client: ApiClient) -> None:
        self.client = client

    async def profile(self) -> Profile:
        envelope = await self.client.request(
            "GET",
            "/auth/current-user-full",
            params={"include": "userInfo,token,isClaimable"},
        )
        data = envelope.dict_data()
        token = data.get("token") if isinstance(data.get("token"), dict) else {}
        user = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else {}
        return Profile(
            username=_first_str(user, "username", "loginId", "name"),
            balance=as_float(token.get("interlinkGoldTokenAmount")),
            last_claim_at=to_epoch_seconds(token.get("lastClaimTime")),
            daily_rate=as_float(token.get("dailyMiningRate")),
            group_rate=as_float(token.get("groupMiningRate")),
            direct_hash_rate=as_float(
                _first(token, "directReferralsHashRate", "directReferralHashRate")
            ),
            indirect_hash_rate=as_float(
                _first(token, "indirectReferralsHashRate", "indirectReferralHashRate")
            ),
            referrals=as_int(token.get("totalReferral")),
            recoverable=as_float(_first(token, "itlgRecoverable", "recoverable")),
            can_recover=bool(token.get("isRecoverable")),
            raw=token,
        )

    async def balance(self) -> float | None:
        """Balance only - used to measure a claim's effect."""
        try:
            return (await self.profile()).balance
        except ApiError as exc:
            log.debug("gagal membaca saldo: %s", exc)
            return None

    async def headroom(self) -> float | None:
        """How much capacity is left in the current 4h frame, if reported."""
        try:
            envelope = await self.client.request("GET", "/token/check-is-claimable")
        except ApiError:
            return None
        data = envelope.dict_data()
        return as_float(data.get("remaining") or data.get("remainingMining"))


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _first_str(mapping: dict[str, Any], *keys: str) -> str | None:
    value = _first(mapping, *keys)
    return str(value) if value is not None else None
