"""OTP parsing: the narrow, testable core of the IMAP reader."""

from __future__ import annotations

import time
from email.message import EmailMessage
from email.utils import formatdate

from itlg_keeper.auth.otp import ImapOtpReader, parse_otp

KEYWORDS = ("login", "verification code", "interlink")


def _message(
    body: str = "Kode login Anda: 483920. Berlaku 10 menit.",
    subject: str = "Interlink Labs login verification code",
    when: float | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "no-reply@interlinklabs.ai"
    message["To"] = "owner@example.com"
    message["Subject"] = subject
    message["Date"] = formatdate(when or time.time(), usegmt=False)
    message["Message-ID"] = "<abc@interlinklabs.ai>"
    message.set_content(body)
    return message.as_bytes()


def test_extracts_six_digit_code():
    parsed = parse_otp(_message(), since=time.time() - 60, keywords=KEYWORDS)
    assert parsed is not None
    assert parsed.code == "483920"
    assert "interlink" in parsed.sender


def test_ignores_old_messages():
    old = time.time() - 3600
    assert parse_otp(_message(when=old), since=time.time() - 60, keywords=KEYWORDS) is None


def test_ignores_unrelated_subject():
    result = parse_otp(
        _message(subject="Weekly newsletter"), since=time.time() - 60, keywords=KEYWORDS
    )
    assert result is None


def test_ignores_message_without_code():
    result = parse_otp(
        _message(body="Silakan buka aplikasi."), since=time.time() - 60, keywords=KEYWORDS
    )
    assert result is None


def test_five_digit_number_is_not_a_code():
    result = parse_otp(
        _message(body="Nomor referensi 12345"), since=time.time() - 60, keywords=KEYWORDS
    )
    assert result is None


def test_reader_requires_imap_credentials(settings):
    broken = settings.account.model_copy(update={"imap_password": None})
    try:
        ImapOtpReader(broken)
    except Exception as exc:
        assert "IMAP" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("reader seharusnya menolak konfigurasi tanpa password")
