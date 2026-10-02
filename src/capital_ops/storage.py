"""联合资本承诺与结算服务的 SQLite 模式和事务辅助。

只持久化参与方目录、仅追加账本事件与幂等结果；一切当前余额与周期对账单
都由 capital_ops.engine 从 ledger_events 重放得到，重启后状态可完整重建。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capital_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('fund_of_funds','market_fund','industrial','gp','operator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE CHECK(length(event_hash) = 64),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_entity
ON ledger_events(entity_type, entity_id, seq);

CREATE INDEX IF NOT EXISTS idx_ledger_round
ON ledger_events(created_at, seq);

CREATE TABLE IF NOT EXISTS capital_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
"""

REQUIRED_TABLES = frozenset({"schema_meta", "capital_users", "ledger_events", "capital_idempotency"})


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    }
