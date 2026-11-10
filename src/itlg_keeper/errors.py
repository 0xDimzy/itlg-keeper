"""Error taxonomy.

The whole point of this module is that callers never have to match on strings
like ``"TOO_EARLY"`` buried inside a message. Every failure the API can produce
is mapped onto exactly one class here, and each class carries enough structure
(status, code, retry hint) for the scheduler to make a decision.
"""

from __future__ import annotations

from typing import Any


class KeeperError(Exception):
    """Base class for every error raised by the keeper."""

    #: Safe to retry without changing state?
    retryable = False


class ConfigError(KeeperError):
    """Configuration is missing or invalid. Never retryable."""


class LedgerError(KeeperError):
    """Local state could not be read or written."""


class AuthError(KeeperError):
    """Credentials were rejected; a human has to log in again."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - representation only
        bits = [self.message]
        if self.code is not None:
            bits.append(f"code={self.code}")
        if self.status is not None:
            bits.append(f"http={self.status}")
        return " | ".join(bits)


class AuthExpired(AuthError):
    """Access token is no longer accepted. A refresh may fix this."""

    retryable = True


class LoginUnavailable(AuthError):
    """No usable credential path is configured (no refresh, no IMAP, no selfie)."""


class ApiError(KeeperError):
    """The server answered, but not with a success envelope."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | str | None = None,
        payload: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload = payload
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - representation only
        bits = [self.message]
        if self.code is not None:
            bits.append(f"code={self.code}")
        if self.status is not None:
            bits.append(f"http={self.status}")
        return " | ".join(bits)


class TransientError(ApiError):
    """Server-side hiccup (5xx, transport, timeout). Retry with backoff."""

    retryable = True


class RateLimited(TransientError):
    """HTTP 429 or an explicit retry hint from the API."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class UpstreamUnavailable(TransientError):
    """Non-JSON body: proxy page, Cloudflare interstitial, gateway timeout.

    This deserves its own class because the previous generation of clients
    silently treated it as "not claimable" and drifted for hours.
    """


class NotClaimable(KeeperError):
    """Nothing to claim yet. Carries the moment it becomes claimable."""

    def __init__(self, message: str, *, due_at: float | None = None) -> None:
        super().__init__(message)
        self.due_at = due_at


class AlreadyClaimed(NotClaimable):
    """The server says the reward was already taken (by us or by the app)."""


class DryRun(KeeperError):
    """Raised when an action would have been performed but dry-run is on."""
