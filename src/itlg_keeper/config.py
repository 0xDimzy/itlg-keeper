"""Configuration loading and validation.

Priority (highest first):

1. environment variables (``ITLG_*``) - the only safe place for secrets on a
   shared host or inside a container;
2. a TOML file (``ITLG_CONFIG`` or ``./itlg-keeper.toml``);
3. built-in defaults.

The device identity is special: it is *stable state*, not configuration. It is
generated once, persisted in the SQLite ledger, and reused forever unless the
operator rotates it explicitly with ``itlg-keeper devices --rotate``.
"""

from __future__ import annotations

import logging
import os
import platform
import secrets
from datetime import timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from .constants import API_BASE, APP_VERSION, DEFAULT_DEVICE_UA, DEVICE_CATALOG
from .errors import ConfigError

try:  # pragma: no cover - exercised on 3.10 only
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("itlg_keeper.config")

CONFIG_FILENAME = "itlg-keeper.toml"
ENV_PREFIX = "ITLG_"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class AccountSettings(BaseModel):
    login_id: str
    passcode: SecretStr
    email: str | None = None
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_password: SecretStr | None = None
    face_photo: Path | None = None
    otp_subject_keywords: tuple[str, ...] = (
        "login",
        "verification code",
        "interlink",
        "otp",
    )

    @field_validator("login_id", mode="before")
    @classmethod
    def _clean_login_id(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("login_id wajib diisi (ID numerik Interlink Anda)")
        return text

    @field_validator("passcode", mode="before")
    @classmethod
    def _clean_passcode(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            return value
        text = str(value).strip().replace(" ", "")
        if not text:
            raise ValueError("passcode wajib diisi")
        return text

    @field_validator("email", mode="before")
    @classmethod
    def _clean_email(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    @field_validator("face_photo", mode="before")
    @classmethod
    def _expand_photo(cls, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        return Path(str(value)).expanduser()

    @property
    def can_use_otp(self) -> bool:
        return bool(self.email and self.imap_password)

    @property
    def can_use_face(self) -> bool:
        return bool(self.face_photo and self.face_photo.is_file())


class TelegramSettings(BaseModel):
    token: SecretStr | None = None
    chat_id: str | None = None
    enabled: bool = True

    @field_validator("chat_id", mode="before")
    @classmethod
    def _clean_chat(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip()

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)


class DeviceIdentity(BaseModel):
    """A frozen fingerprint. Changing it invalidates the stored session."""

    device_id: str
    brand: str
    model: str
    user_agent: str = DEFAULT_DEVICE_UA


class RuntimeSettings(BaseModel):
    api_base: str = API_BASE
    app_version: str = APP_VERSION
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".itlg-keeper")
    timezone: str = "Asia/Jakarta"
    jitter_min: int = 45
    jitter_max: int = 180
    request_timeout: float = 30.0
    settle_seconds: float = 20.0
    max_retries: int = 3
    dry_run: bool = False
    log_level: str = "INFO"
    telegram_commands: bool = True

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_dir(cls, value: Any) -> Path:
        return Path(str(value)).expanduser()

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        """Reject nonsense, but tolerate a host without a tz database.

        Windows ships no zoneinfo and needs the ``tzdata`` package; if it is
        missing we fall back to UTC at render time instead of refusing to start.
        """
        if not value.strip():
            raise ValueError("timezone tidak boleh kosong")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.warning(
                "timezone %r tidak tersedia di sistem ini (pasang paket tzdata); "
                "menampilkan waktu dalam UTC",
                value,
            )
        return value

    @model_validator(mode="after")
    def _jitter_order(self) -> RuntimeSettings:
        if self.jitter_min < 0 or self.jitter_max < 0:
            raise ValueError("jitter tidak boleh negatif")
        if self.jitter_min > self.jitter_max:
            raise ValueError("jitter_min tidak boleh lebih besar dari jitter_max")
        return self

    @property
    def tz(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return timezone.utc


class Settings(BaseModel):
    account: AccountSettings
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    device: DeviceIdentity | None = None
    source: Path | None = None

    @property
    def ledger_path(self) -> Path:
        return self.runtime.data_dir / "keeper.db"

    def require_device(self) -> DeviceIdentity:
        if self.device is None:
            raise ConfigError("identitas perangkat belum dibuat; jalankan `itlg-keeper devices --rotate`")
        return self.device

    def with_device(self, device: DeviceIdentity) -> Settings:
        return self.model_copy(update={"device": device})


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def default_data_dir() -> Path:
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "itlg-keeper"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "itlg-keeper"
    return Path.home() / ".local" / "share" / "itlg-keeper"


def resolve_config_path(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        candidate = explicit.expanduser()
        if not candidate.is_file():
            raise ConfigError(f"file konfigurasi tidak ditemukan: {candidate}")
        return candidate
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        candidate = Path(env_path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"ITLG_CONFIG menunjuk ke file yang tidak ada: {candidate}")
        return candidate
    for candidate in (Path.cwd() / CONFIG_FILENAME, default_data_dir() / CONFIG_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML tidak valid di {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"tidak bisa membaca {path}: {exc}") from exc


def _env(name: str) -> str | None:
    value = os.environ.get(f"{ENV_PREFIX}{name}")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    raw = _env(name)
    if raw is None:
        return None
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str) -> int | None:
    raw = _env(name)
    return int(raw) if raw is not None else None


def generate_device() -> DeviceIdentity:
    """Create a plausible, stable device fingerprint."""
    brand, model, ua = secrets.choice(DEVICE_CATALOG)
    return DeviceIdentity(
        device_id=secrets.token_hex(8),
        brand=brand,
        model=model,
        user_agent=ua,
    )


def load_settings(
    config_path: Path | None = None,
    *,
    require_device: bool = False,
) -> Settings:
    path = resolve_config_path(config_path)
    raw = _read_toml(path)

    account_raw = dict(raw.get("account") or {})
    runtime_raw = dict(raw.get("runtime") or {})
    telegram_raw = dict(raw.get("telegram") or {})
    device_raw = raw.get("device")

    env_account = {
        "login_id": _env("LOGIN_ID"),
        "passcode": _env("PASSCODE"),
        "email": _env("EMAIL"),
        "imap_host": _env("IMAP_HOST"),
        "imap_password": _env("IMAP_PASSWORD"),
        "face_photo": _env("FACE_PHOTO"),
    }
    env_runtime = {
        "api_base": _env("API_BASE"),
        "data_dir": _env("DATA_DIR"),
        "timezone": _env("TIMEZONE"),
        "jitter_min": _env_int("JITTER_MIN"),
        "jitter_max": _env_int("JITTER_MAX"),
        "settle_seconds": _env("SETTLE_SECONDS"),
        "max_retries": _env_int("MAX_RETRIES"),
        "log_level": _env("LOG_LEVEL"),
        "dry_run": _env_bool("DRY_RUN"),
        "telegram_commands": _env_bool("TELEGRAM_COMMANDS"),
    }
    env_telegram = {
        "token": _env("TELEGRAM_TOKEN"),
        "chat_id": _env("TELEGRAM_CHAT_ID"),
        "enabled": _env_bool("TELEGRAM_ENABLED"),
    }

    for key, value in env_account.items():
        if value is not None:
            account_raw[key] = value
    for key, value in env_runtime.items():
        if value is not None:
            runtime_raw[key] = value
    for key, value in env_telegram.items():
        if value is not None:
            telegram_raw[key] = value

    runtime_raw.setdefault("data_dir", default_data_dir())

    try:
        account = AccountSettings.model_validate(account_raw)
    except Exception as exc:
        raise ConfigError(f"konfigurasi akun tidak valid: {exc}") from exc

    try:
        runtime = RuntimeSettings.model_validate(runtime_raw)
    except Exception as exc:
        raise ConfigError(f"konfigurasi runtime tidak valid: {exc}") from exc

    try:
        telegram = TelegramSettings.model_validate(telegram_raw)
    except Exception as exc:
        raise ConfigError(f"konfigurasi telegram tidak valid: {exc}") from exc

    device = DeviceIdentity.model_validate(device_raw) if device_raw else None
    settings = Settings(
        account=account,
        runtime=runtime,
        telegram=telegram,
        device=device,
        source=path,
    )
    if require_device and settings.device is None:
        raise ConfigError("identitas perangkat belum ada di konfigurasi")
    return settings


def masked_dump(settings: Settings) -> dict[str, Any]:
    """Config for display/logging with every secret replaced."""
    data = settings.model_dump(mode="json")
    for section, keys in (
        ("account", ("passcode", "imap_password")),
        ("telegram", ("token",)),
    ):
        block = data.get(section) or {}
        for key in keys:
            if block.get(key):
                block[key] = "***"
    return data
