"""OTP retrieval over IMAP.

The login code is emailed, so the keeper has to read the mailbox. This module
keeps that job narrow: parse a message into a code, and poll politely until one
appears. All IMAP I/O happens in a worker thread so the event loop keeps
running (the old implementation blocked every other claim while waiting).
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from ..config import AccountSettings
from ..constants import OTP_POLL_INTERVAL, OTP_TIMEOUT
from ..errors import AuthError

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@dataclass(slots=True)
class OtpMessage:
    code: str
    subject: str
    sender: str
    received_at: float
    message_id: str


def _body_text(message: Message) -> str:
    """Concatenate the readable parts of a multipart message."""
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = message.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def parse_otp(
    raw: bytes,
    *,
    since: float,
    keywords: tuple[str, ...],
    slack: float = 90.0,
) -> OtpMessage | None:
    """Return the newest plausible OTP in ``raw``, or ``None``.

    A message qualifies when it arrived after ``since`` (minus a small slack for
    clock drift), its subject mentions one of the keywords, and its body holds
    exactly one six-digit group.
    """
    message = email.message_from_bytes(raw)
    subject = str(message.get("Subject") or "")
    sender = str(message.get("From") or "")

    received = _received_at(message)
    if received is None or received < since - slack:
        return None
    if keywords and not any(word.lower() in subject.lower() for word in keywords):
        return None

    body = _body_text(message)
    match = _CODE_RE.search(body) or _CODE_RE.search(subject)
    if match is None:
        return None
    return OtpMessage(
        code=match.group(1),
        subject=subject,
        sender=sender,
        received_at=received,
        message_id=str(message.get("Message-ID") or f"{subject}:{received}"),
    )


def _received_at(message: Message) -> float | None:
    header = message.get("Date")
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(str(header))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class ImapOtpReader:
    """Polls a Gmail inbox (or any IMAPS host) for the newest login code."""

    def __init__(
        self,
        account: AccountSettings,
        *,
        timeout: float = OTP_TIMEOUT,
        poll_interval: float = OTP_POLL_INTERVAL,
        scan_limit: int = 12,
    ) -> None:
        if not account.can_use_otp:
            raise AuthError("IMAP belum dikonfigurasi: isi email dan app password")
        self.account = account
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.scan_limit = scan_limit

    async def wait_for_code(self, *, since: float, exclude: set[str] | None = None) -> str:
        exclude = exclude or set()
        deadline = time.time() + self.timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                messages = await asyncio.to_thread(self._scan, since)
            except AuthError:
                raise
            except Exception as exc:
                last_error = exc
                messages = []
            for message in messages:
                if message.code in exclude:
                    continue
                return message.code
            await asyncio.sleep(min(self.poll_interval, max(1.0, deadline - time.time())))
        detail = f" (error terakhir: {last_error})" if last_error else ""
        raise AuthError(f"kode OTP tidak ditemukan dalam {self.timeout:.0f}s{detail}")

    # -- blocking internals ------------------------------------------------
    def _scan(self, since: float) -> list[OtpMessage]:
        password = self.account.imap_password
        if password is None:  # pragma: no cover - guarded by can_use_otp
            raise AuthError("app password IMAP kosong")
        assert self.account.email is not None
        since_date = time.strftime("%d-%b-%Y", time.localtime(since - 86400))
        try:
            with imaplib.IMAP4_SSL(self.account.imap_host, self.account.imap_port, timeout=30) as imap:
                imap.login(self.account.email, password.get_secret_value())
                imap.select("INBOX")
                status, data = imap.uid("search", None, "SINCE", since_date)
                if status != "OK" or not data or data[0] is None:
                    return []
                uids = data[0].split()[-self.scan_limit :]
                results: list[OtpMessage] = []
                for uid in reversed(uids):
                    status, fetched = imap.uid("fetch", uid, "(RFC822)")
                    if status != "OK" or not fetched:
                        continue
                    raw = next(
                        (part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1),
                        None,
                    )
                    if not isinstance(raw, bytes):
                        continue
                    parsed = parse_otp(
                        raw, since=since, keywords=self.account.otp_subject_keywords
                    )
                    if parsed is not None:
                        results.append(parsed)
                return results
        except imaplib.IMAP4.error as exc:
            raise AuthError(
                "login IMAP ditolak. Pastikan 2-Step Verification aktif dan gunakan "
                f"App Password Gmail (bukan password akun): {exc}"
            ) from exc
        except OSError as exc:
            raise AuthError(f"tidak bisa terhubung ke {self.account.imap_host}: {exc}") from exc


def looks_like_otp(text: str) -> bool:
    return bool(_CODE_RE.search(text))


def utc_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
