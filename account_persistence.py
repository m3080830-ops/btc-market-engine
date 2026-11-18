"""Tenant-isolated persistence for the staged multi-account migration.

Each (account_id, symbol) receives a separate SQLite database file. This
module is deliberately standalone: it is not wired into main.py or the live
MEXC adapter. It stores execution data only, never API credentials.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from account_execution import ExecutionScope


_RECORD_TYPES = frozenset({
    "trade", "order", "position", "stop", "balance_snapshot",
    "execution_state", "audit",
})
_FORBIDDEN_KEYS = frozenset({
    "access_key", "secret_key", "api_key", "api_secret",
    "apikey", "apisecret", "password", "authorization",
})


def _validate_payload(value: Any, path: str = "payload") -> None:
    """Reject credential-like fields recursively before persistence."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            compact = normalized.replace("_", "")
            if normalized in _FORBIDDEN_KEYS or compact in _FORBIDDEN_KEYS:
                raise ValueError(f"credential-like field is not persistable: {path}.{key}")
            _validate_payload(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_payload(child, f"{path}[{index}]")


def _json_payload(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")
    _validate_payload(payload)
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise ValueError("payload must contain finite JSON-compatible values") from None


class SQLiteAccountRepository:
    """Repository permanently bound to one validated account/instrument scope."""

    def __init__(self, db_path: str, scope: ExecutionScope):
        if not isinstance(scope, ExecutionScope):
            raise TypeError("scope must be an ExecutionScope")
        self.scope = scope
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS repository_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL
                )
            """)
            identity = conn.execute(
                "SELECT account_id, symbol FROM repository_identity WHERE singleton=1"
            ).fetchone()
            expected = (self.scope.account_id, self.scope.symbol)
            if identity is None:
                conn.execute(
                    "INSERT INTO repository_identity(singleton, account_id, symbol) VALUES(1, ?, ?)",
                    expected,
                )
            elif (identity["account_id"], identity["symbol"]) != expected:
                raise ValueError("database file is already bound to a different execution scope")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_state (
                    state_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_records (
                    record_type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY(record_type, record_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                )
            """)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def set_state(self, key: str, value: Any) -> None:
        key = str(key).strip()
        if not key:
            raise ValueError("state key is required")
        encoded = _json_payload({"value": value})
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO account_state(state_key, value_json, updated_at)
                   VALUES(?, ?, unixepoch())
                   ON CONFLICT(state_key) DO UPDATE SET
                     value_json=excluded.value_json, updated_at=unixepoch()""",
                (key, encoded),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM account_state WHERE state_key=?",
                (str(key),),
            ).fetchone()
        return json.loads(row["value_json"])["value"] if row else default

    def put_record(self, record_type: str, record_id: str, payload: dict) -> None:
        record_type = str(record_type).strip().lower()
        record_id = str(record_id).strip()
        if record_type not in _RECORD_TYPES:
            raise ValueError("unsupported execution record type")
        if not record_id or len(record_id) > 256:
            raise ValueError("record_id must contain 1 to 256 characters")
        encoded = _json_payload(payload)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO execution_records
                   (record_type, record_id, payload_json)
                   VALUES(?, ?, ?)
                   ON CONFLICT(record_type, record_id) DO UPDATE SET
                     payload_json=excluded.payload_json, updated_at=unixepoch()""",
                (record_type, record_id, encoded),
            )

    def get_record(self, record_type: str, record_id: str):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM execution_records
                   WHERE record_type=? AND record_id=?""",
                (str(record_type).strip().lower(), str(record_id).strip()),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_records(self, record_type: str):
        record_type = str(record_type).strip().lower()
        if record_type not in _RECORD_TYPES:
            raise ValueError("unsupported execution record type")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT record_id, payload_json, created_at, updated_at
                   FROM execution_records WHERE record_type=?
                   ORDER BY created_at, record_id""",
                (record_type,),
            ).fetchall()
        return [
            {
                "record_id": row["record_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def append_event(self, event_id: str, event_type: str, payload: dict) -> bool:
        """Append an immutable event once; return False for a replayed event ID."""
        event_id = str(event_id).strip()
        event_type = str(event_type).strip()
        if not event_id or len(event_id) > 256 or not event_type:
            raise ValueError("event_id and event_type are required")
        encoded = _json_payload(payload)
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO execution_events
                   (event_id, event_type, payload_json) VALUES(?, ?, ?)""",
                (event_id, event_type, encoded),
            )
            return cursor.rowcount == 1

    def list_events(self, limit: int = 100):
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event_id, event_type, payload_json, created_at
                   FROM execution_events ORDER BY created_at DESC, event_id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


class AccountRepositoryFactory:
    """Create deterministic, non-guessable per-scope database paths."""

    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root_dir, 0o700)
        except OSError:
            pass

    def database_path(self, scope: ExecutionScope) -> str:
        if not isinstance(scope, ExecutionScope):
            raise TypeError("scope must be an ExecutionScope")
        identity = f"{scope.account_id}\0{scope.symbol}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        return str(self.root_dir / f"{digest}.sqlite3")

    def for_scope(self, scope: ExecutionScope) -> SQLiteAccountRepository:
        return SQLiteAccountRepository(self.database_path(scope), scope)
