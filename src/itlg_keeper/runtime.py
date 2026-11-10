"""Composition root: builds the object graph once and hands it to the CLI,
the scheduler and the Telegram bot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .auth.login import LoginService
from .auth.tokens import TokenManager, TokenPair
from .config import DeviceIdentity, Settings, generate_device
from .errors import AuthExpired
from .http import ApiClient
from .ledger import Ledger
from .notify import Notifier
from .services.account import AccountService
from .services.groups import GroupService
from .services.mining import MiningService
from .services.recovery import RecoveryService

log = logging.getLogger("itlg_keeper.runtime")

DEVICE_KEY = "device"


async def resolve_device(settings: Settings, ledger: Ledger) -> Settings:
    """Use the configured device, else the stored one, else mint a new one.

    The identity must never change silently: the server binds sessions to it.
    """
    if settings.device is not None:
        ledger.put(DEVICE_KEY, settings.device.model_dump())
        return settings

    stored = ledger.get(DEVICE_KEY)
    if isinstance(stored, dict):
        try:
            device = DeviceIdentity.model_validate(stored)
        except Exception:
            log.warning("identitas perangkat rusak di database; membuat baru")
        else:
            return settings.with_device(device)

    device = generate_device()
    ledger.put(DEVICE_KEY, device.model_dump())
    ledger.event(
        "info",
        "device",
        f"identitas perangkat baru: {device.brand} {device.model} / {device.device_id}",
    )
    log.info("identitas perangkat dibuat: %s %s (%s)", device.brand, device.model, device.device_id)
    return settings.with_device(device)


@dataclass
class AppContext:
    settings: Settings
    ledger: Ledger
    tokens: TokenManager
    client: ApiClient
    login: LoginService
    account: AccountService
    mining: MiningService
    groups: GroupService
    recovery: RecoveryService
    notifier: Notifier
    started_at: float

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        telegram_transport: httpx.AsyncBaseTransport | None = None,
    ) -> AppContext:
        ledger = Ledger(settings.ledger_path)
        settings = await resolve_device(settings, ledger)

        tokens = TokenManager(ledger)
        client = ApiClient(settings, tokens, transport=transport)
        login = LoginService(client, tokens, ledger, settings)

        async def refresh(refresh_token: str) -> TokenPair:
            envelope = await client.request(
                "POST", "/auth/token", body={"refreshToken": refresh_token}, auth=False
            )
            data = envelope.dict_data()
            access = data.get("accessToken") or data.get("jwtToken")
            if not access and isinstance(data.get("token"), str):
                access = data["token"]
            if not access:
                raise AuthExpired("refresh token diterima tetapi tanpa access token")
            from .auth.tokens import decode_expiry

            expires = decode_expiry(str(access))
            return TokenPair(
                access=str(access),
                refresh=str(data.get("refreshToken")) if data.get("refreshToken") else refresh_token,
                expires_at=expires if expires is not None else time.time() + 1800,
                source="refresh",
            )

        tokens.bind(refresher=refresh, login=login.ensure_login)

        account = AccountService(client)
        return cls(
            settings=settings,
            ledger=ledger,
            tokens=tokens,
            client=client,
            login=login,
            account=account,
            mining=MiningService(client, account, ledger, settings),
            groups=GroupService(client, account, ledger, settings),
            recovery=RecoveryService(
                client,
                account,
                ledger,
                settle_seconds=settings.runtime.settle_seconds,
            ),
            notifier=Notifier(settings.telegram, transport=telegram_transport),
            started_at=time.time(),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.notifier.aclose()
        self.ledger.close()
