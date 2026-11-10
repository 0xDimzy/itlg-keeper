"""Static values that describe the Interlink Labs mobile client.

These are protocol constants, not tunables: the server validates the bundle id,
the version pair and the Android platform hint. Everything that *is* tunable
lives in :mod:`itlg_keeper.config`.
"""

from __future__ import annotations

APP_NAME = "itlg-keeper"
VERSION = "1.0.0"

# --- Remote API -------------------------------------------------------------
API_BASE = "https://prod.interlinklabs.ai/api/v1"
APP_VERSION = "5.0.5"
BUNDLE_ID = "org.ai.interlinklabs.interlinkId"
PLATFORM = "android"
SYSTEM_NAME = "Android"

# --- Cadence ----------------------------------------------------------------
MINING_INTERVAL = 4 * 60 * 60
GROUP_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 5 * 60
RECOVERY_INTERVAL = 30 * 60

# --- Auth -------------------------------------------------------------------
OTP_TIMEOUT = 240.0
OTP_POLL_INTERVAL = 4.0
OTP_MAX_ATTEMPTS = 3
TOKEN_SKEW = 120.0
# Access tokens that are not JWTs (opaque strings) cannot expose an expiry.
# Instead of re-logging-in on every call - which is what a naive parser does -
# we assume a conservative lifetime and let a 401 force the refresh.
OPAQUE_TOKEN_TTL = 30 * 60

# --- Device identities ------------------------------------------------------
# (brand, model, user_agent). The UA is kept stable per identity: rotating it
# per request inside one session looks less like a phone, not more.
DEVICE_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("XiaoMi", "M2101K6G", "okhttp/4.12.0"),
    ("XiaoMi", "Redmi Note 11", "okhttp/4.12.0"),
    ("samsung", "SM-G991B", "okhttp/4.12.0"),
    ("samsung", "SM-A525F", "okhttp/4.12.0"),
    ("Google", "Pixel 6", "okhttp/4.12.0"),
    ("Google", "Pixel 7", "okhttp/4.12.0"),
    ("OPPO", "CPH2247", "okhttp/4.12.0"),
    ("vivo", "V2057A", "okhttp/4.12.0"),
    ("Realme", "RMX3081", "okhttp/4.12.0"),
    ("POCO", "M2101K6G", "okhttp/4.12.0"),
)

DEFAULT_DEVICE_UA = "okhttp/4.12.0"

TELEGRAM_API = "https://api.telegram.org"
