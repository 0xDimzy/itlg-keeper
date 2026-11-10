"""Login flows: email OTP and selfie verification.

Both paths end in the same place - an access token plus a refresh token - so
the rest of the keeper only ever deals with :class:`TokenPair`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import Settings
from ..constants import OTP_MAX_ATTEMPTS
from ..errors import ApiError, AuthError, LoginUnavailable
from ..http import ApiClient
from ..ledger import Ledger
from .otp import ImapOtpReader
from .tokens import TokenManager, TokenPair, decode_expiry

log = logging.getLogger("itlg_keeper.login")

_MIN_PHOTO_BYTES = 50_000
_MAX_PHOTO_BYTES = 5_000_000


class LoginService:
    def __init__(
        self,
        client: ApiClient,
        tokens: TokenManager,
        ledger: Ledger,
        settings: Settings,
    ) -> None:
        self.client = client
        self.tokens = tokens
        self.ledger = ledger
        self.settings = settings
        self.account = settings.account
        self.device = settings.require_device()

    # -- shared pre-flight -------------------------------------------------
    async def _assert_login_id_exists(self) -> None:
        self.device = self.settings.require_device()
        envelope = await self.client.request(
            "GET",
            f"/auth/loginId-exist-check/{self.account.login_id}",
            params={"deviceId": self.settings.require_device().device_id},
            auth=False,
        )
        if envelope.data is False:
            raise AuthError(
                f"login ID {self.account.login_id} tidak terdaftar di Interlink Labs"
            )

    async def check_passcode(self) -> str | None:
        """Validate the passcode and discover the email bound to the account."""
        envelope = await self.client.request(
            "POST",
            "/auth/check-passcode",
            body={
                "loginId": self.account.login_id,
                "passcode": self.account.passcode.get_secret_value(),
                "deviceId": self.settings.require_device().device_id,
            },
            params={"v": "2"},
            auth=False,
        )
        data = envelope.dict_data()
        email = data.get("email")
        if not email:
            verification = data.get("verificationInfo")
            if isinstance(verification, list) and verification:
                first = verification[0]
                if isinstance(first, dict):
                    email = first.get("gmail") or first.get("email")
        return str(email) if email else None

    # -- OTP ---------------------------------------------------------------
    async def login_with_otp(self) -> TokenPair:
        if not self.account.can_use_otp:
            raise LoginUnavailable(
                "login OTP butuh email + App Password Gmail. Isi ITLG_EMAIL dan "
                "ITLG_IMAP_PASSWORD, atau pakai login selfie."
            )
        await self._assert_login_id_exists()
        discovered = await self.check_passcode()
        email = discovered or self.account.email
        if not email:
            raise AuthError("email akun tidak diketahui; isi bagian email di konfigurasi")

        reader = ImapOtpReader(self.account)
        used_codes: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(1, OTP_MAX_ATTEMPTS + 1):
            log.info("permintaan OTP (percobaan %d/%d)", attempt, OTP_MAX_ATTEMPTS)
            sent_at = time.time()
            await self.client.request(
                "POST",
                "/auth/send-otp-email-verify-login",
                body={
                    "loginId": self.account.login_id,
                    "passcode": self.account.passcode.get_secret_value(),
                    "email": email,
                    "deviceId": self.settings.require_device().device_id,
                },
                auth=False,
            )
            try:
                code = await reader.wait_for_code(since=sent_at, exclude=used_codes)
            except AuthError as exc:
                last_error = exc
                log.warning("OTP belum datang: %s", exc)
                continue
            used_codes.add(code)
            try:
                envelope = await self.client.request(
                    "POST",
                    "/auth/check-otp-email-verify-login",
                    body={
                        "loginId": self.account.login_id,
                        "otp": code,
                        "deviceId": self.settings.require_device().device_id,
                    },
                    params={"v": "2"},
                    auth=False,
                )
            except ApiError as exc:
                last_error = exc
                log.warning("kode OTP ditolak: %s", exc)
                continue
            pair = _tokens_from(envelope.data, envelope.dict_data(), source="otp")
            if pair is None:
                last_error = AuthError("respons verifikasi tidak memuat token")
                continue
            self.ledger.event("info", "login", "login OTP berhasil")
            return pair

        raise AuthError(f"login OTP gagal setelah {OTP_MAX_ATTEMPTS} percobaan: {last_error}")

    # -- selfie ------------------------------------------------------------
    async def login_with_face(self, photo: str | None = None) -> TokenPair:
        path = photo or (str(self.account.face_photo) if self.account.face_photo else None)
        if not path:
            raise LoginUnavailable("login selfie butuh file foto (ITLG_FACE_PHOTO)")
        from pathlib import Path

        photo_path = Path(path).expanduser()  # noqa: ASYNC240 - one-off login syscalls
        if not photo_path.is_file():
            raise LoginUnavailable(f"file foto tidak ditemukan: {photo_path}")
        content = await asyncio.to_thread(photo_path.read_bytes)
        if not _MIN_PHOTO_BYTES <= len(content) <= _MAX_PHOTO_BYTES:
            raise LoginUnavailable(
                f"ukuran foto {len(content) // 1024}KB di luar batas "
                f"{_MIN_PHOTO_BYTES // 1000}KB-{_MAX_PHOTO_BYTES // 1_000_000}MB"
            )
        content_type = _sniff_image_type(content)
        if content_type is None:
            raise LoginUnavailable("foto harus JPEG atau PNG yang valid")

        await self._assert_login_id_exists()
        await self.check_passcode()

        envelope = await self.client.request(
            "POST",
            "/s3/face/presigned-login",
            body={
                "loginId": self.account.login_id,
                "passcode": self.account.passcode.get_secret_value(),
            },
            auth=False,
        )
        data = envelope.dict_data()
        image = data.get("image") if isinstance(data.get("image"), dict) else {}
        key = image.get("key") or data.get("key")
        upload_url = image.get("uploadUrl") or data.get("uploadUrl")
        if not key or not upload_url:
            raise AuthError("server tidak mengembalikan target unggah foto")

        log.info("mengunggah selfie (%d KB, %s)", len(content) // 1024, content_type)
        await self.client.put_bytes(str(upload_url), content, content_type)

        login_env = await self.client.request(
            "POST",
            "/auth/login",
            body={
                "loginId": self.account.login_id,
                "passcode": self.account.passcode.get_secret_value(),
                "image": key,
                "presignedUrlImage": key,
            },
            auth=False,
        )
        pair = _tokens_from(login_env.data, login_env.dict_data(), source="face")
        if pair is None:
            raise AuthError("login selfie tidak mengembalikan token")
        self.ledger.event("info", "login", "login selfie berhasil")
        return pair

    # -- entry point used by the token manager -----------------------------
    async def ensure_login(self) -> TokenPair:
        errors: list[str] = []
        if self.account.can_use_face:
            try:
                return await self.login_with_face()
            except (LoginUnavailable, AuthError, ApiError) as exc:
                errors.append(f"selfie: {exc}")
                log.warning("login selfie gagal: %s", exc)
        if self.account.can_use_otp:
            try:
                return await self.login_with_otp()
            except (LoginUnavailable, AuthError, ApiError) as exc:
                errors.append(f"otp: {exc}")
                log.warning("login OTP gagal: %s", exc)
        detail = " | ".join(errors) if errors else "tidak ada metode login yang dikonfigurasi"
        raise LoginUnavailable(f"semua jalur login gagal: {detail}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tokens_from(data: Any, payload: dict[str, Any], *, source: str) -> TokenPair | None:
    """Extract tokens from whichever shape the server chose today."""
    candidates: list[dict[str, Any]] = []
    if isinstance(data, dict):
        candidates.append(data)
        nested = data.get("token")
        if isinstance(nested, dict):
            candidates.append(nested)
    candidates.append(payload)

    access = None
    refresh = None
    for candidate in candidates:
        access = (
            access
            or candidate.get("accessToken")
            or candidate.get("access_token")
            or candidate.get("jwtToken")
            or (candidate.get("token") if isinstance(candidate.get("token"), str) else None)
        )
        refresh = refresh or candidate.get("refreshToken") or candidate.get("refresh_token")
        if access and refresh:
            break
    if not access:
        return None
    expires = decode_expiry(str(access))
    return TokenPair(
        access=str(access),
        refresh=str(refresh) if refresh else None,
        expires_at=expires if expires is not None else time.time() + 1800,
        source=source,
    )


def _sniff_image_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None
