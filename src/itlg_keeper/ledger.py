"""Durable local state, kept in one SQLite database.

Why a database instead of the traditional pile of JSON files:

* atomic writes - a kill -9 in the middle of a claim cannot corrupt state;
* one file to back up, one file to chmod;
* the claim history is queryable, so ``status`` and ``stats`` do not have to
  guess an average from a truncated list;
* concurrent readers (CLI while the daemon runs) are safe thanks to WAL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LedgerError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    access            TEXT,
    refresh           TEXT,
    access_expires_at REAL,
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    amount          REAL,
    balance_before  REAL,
    balance_after   REAL,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT,
    payload         TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_created ON claims(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_claims_kind ON claims(kind, created_at DESC);
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    scope      TEXT NOT NULL,
    message    TEXT NOT NULL,
    payload    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
"""


@dataclass(slots=True)
class StoredToken:
    access: str
    refresh: str | None
    expires_at: float | None
    updated_at: float


@dataclass(slots=True)
class ClaimRecord:
    id: int
    kind: str
    amount: float | None
    balance_before: float | None
    balance_after: float | None
    source: str
    status: str
    message: str | None
    created_at: float


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        except (OSError, sqlite3.Error) as exc:
            raise LedgerError(f"tidak bisa membuka database {path}: {exc}") from exc
        self._db.row_factory = sqlite3.Row
        self._lock_db()
        self._install_schema()

    # -- infrastructure ----------------------------------------------------
    def _lock_db(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=15000")

    def _install_schema(self) -> None:
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                cursor = self._db.execute(sql, params)
                self._db.commit()
                return cursor
            except sqlite3.Error as exc:
                raise LedgerError(f"operasi database gagal: {exc}") from exc

    # -- tokens ------------------------------------------------------------
    def load_token(self) -> StoredToken | None:
        row = self._execute("SELECT access, refresh, access_expires_at, updated_at FROM tokens WHERE id=1").fetchone()
        if row is None or not row["access"]:
            return None
        return StoredToken(
            access=row["access"],
            refresh=row["refresh"],
            expires_at=row["access_expires_at"],
            updated_at=row["updated_at"],
        )

    def save_token(self, access: str, refresh: str | None, expires_at: float | None) -> None:
        self._execute(
            "INSERT INTO tokens(id, access, refresh, access_expires_at, updated_at) "
            "VALUES(1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "access=excluded.access, refresh=excluded.refresh, "
            "access_expires_at=excluded.access_expires_at, updated_at=excluded.updated_at",
            (access, refresh, expires_at, time.time()),
        )

    def clear_token(self) -> None:
        self._execute("DELETE FROM tokens WHERE id=1")

    # -- key/value ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def put(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, separators=(",", ":")), time.time()),
        )

    def delete(self, key: str) -> None:
        self._execute("DELETE FROM kv WHERE key=?", (key,))

    # -- claims ------------------------------------------------------------
    def record_claim(
        self,
        *,
        kind: str,
        status: str,
        amount: float | None = None,
        balance_before: float | None = None,
        balance_after: float | None = None,
        source: str = "api",
        message: str | None = None,
        payload: Any = None,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO claims(kind, amount, balance_before, balance_after, source, status, message, payload, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                amount,
                balance_before,
                balance_after,
                source,
                status,
                message,
                json.dumps(payload, default=str)[:8000] if payload is not None else None,
                time.time(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def recent_claims(self, limit: int = 20, kind: str | None = None) -> list[ClaimRecord]:
        if kind:
            rows = self._execute(
                "SELECT * FROM claims WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = self._execute("SELECT * FROM claims ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            ClaimRecord(
                id=r["id"],
                kind=r["kind"],
                amount=r["amount"],
                balance_before=r["balance_before"],
                balance_after=r["balance_after"],
                source=r["source"],
                status=r["status"],
                message=r["message"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def claim_totals(self, since: float) -> dict[str, Any]:
        rows = self._execute(
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total "
            "FROM claims WHERE status='claimed' AND created_at >= ? GROUP BY kind",
            (since,),
        ).fetchall()
        by_kind = {r["kind"]: {"count": r["n"], "amount": r["total"]} for r in rows}
        total = self._execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM claims "
            "WHERE status='claimed' AND created_at >= ?",
            (since,),
        ).fetchone()
        return {
            "since": since,
            "count": total["n"] if total else 0,
            "amount": float(total["total"]) if total else 0.0,
            "by_kind": by_kind,
        }

    def last_claim_at(self, kind: str) -> float | None:
        row = self._execute(
            "SELECT created_at FROM claims WHERE kind=? AND status='claimed' ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return float(row["created_at"]) if row else None

    # -- events ------------------------------------------------------------
    def event(self, level: str, scope: str, message: str, payload: Any = None) -> None:
        self._execute(
            "INSERT INTO events(level, scope, message, payload, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                level,
                scope,
                message,
                json.dumps(payload, default=str)[:8000] if payload is not None else None,
                time.time(),
            ),
        )

    def recent_events(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT level, scope, message, created_at FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- housekeeping ------------------------------------------------------
    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for table in ("claims", "events"):
            cursor = self._execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            removed += cursor.rowcount or 0
        return removed

    def vacuum(self) -> None:
        with self._lock:
            self._db.execute("VACUUM")
