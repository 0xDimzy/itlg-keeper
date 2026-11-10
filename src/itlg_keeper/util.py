"""Small shared helpers: clocks, coercion, formatting, async waiting."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import tempfile
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

#: Any epoch value above this is milliseconds, not seconds (year 5138 in ms).
_MS_THRESHOLD = 1e11


def now_ms() -> int:
    return int(time.time() * 1000)


def to_epoch_seconds(value: Any) -> float | None:
    """Normalise an API timestamp to seconds.

    The API is inconsistent by nature: some fields are milliseconds, some are
    seconds, some arrive as strings, and a missing field is ``None``. Guessing
    wrong by a factor of 1000 once caused a claim loop that hammered the
    endpoint every tick, so the unit is decided explicitly here.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number > _MS_THRESHOLD:
        number /= 1000.0
    return number


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "ok", "success"}
    return False


def to_local(epoch: float | None, tz: tzinfo | None = None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz).strftime("%d %b %H:%M:%S")


def humanize(seconds: float | None) -> str:
    """Compact countdown: ``3j 12m`` / ``12m 05s`` / ``sekarang``."""
    if seconds is None:
        return "-"
    if seconds <= 0:
        return "sekarang"
    seconds = round(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}h {hours}j"
    if hours:
        return f"{hours}j {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


async def sleep_until(stop: asyncio.Event, deadline: float) -> bool:
    """Sleep until the wall-clock ``deadline``; False when ``stop`` fires first."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return not stop.is_set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=remaining)
            return False
        except asyncio.TimeoutError:
            return True


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` without leaving a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


#: Fields that a claim response may use to report the reward. The API has been
#: observed to return none of them, but when one appears it is trusted over a
#: balance delta because it is exact.
_AMOUNT_KEYS = (
    "amount",
    "reward",
    "claimed",
    "claimedAmount",
    "totalClaim",
    "totalClaimed",
    "itlgAmount",
    "interlinkGoldTokenAmount",
    "tokenAmount",
    "value",
)


def pick_amount(payload: Any, keys: tuple[str, ...] = _AMOUNT_KEYS) -> float | None:
    """Find the first numeric reward field in a nested response."""
    if isinstance(payload, dict):
        for key in keys:
            value = as_float(payload.get(key))
            if value is not None and value > 0:
                return value
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = pick_amount(value, keys)
                if found is not None:
                    return found
    elif isinstance(payload, list):
        for item in payload:
            found = pick_amount(item, keys)
            if found is not None:
                return found
    return None

