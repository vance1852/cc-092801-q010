"""联合资本承诺与结算服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS capital_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','party','risk','auditor')),
    party_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES capital_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreement_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    round_code TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    basis_revision_id INTEGER REFERENCES agreement_revisions(revision_id),
    reason TEXT,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'effective'
        CHECK(state IN ('draft','effective','superseded')),
    created_by TEXT NOT NULL REFERENCES capital_users(user_id),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(project_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_revisions_project
ON agreement_revisions(project_id, revision);

CREATE TABLE IF NOT EXISTS project_addendums (
    addendum_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    previous_revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    next_revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    adjustment_kind TEXT NOT NULL
        CHECK(adjustment_kind IN ('round_add','party_add','party_exit','term_change','restart')),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES capital_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS party_commitments (
    commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    party_id TEXT NOT NULL,
    party_kind TEXT NOT NULL,
    committed_amount TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','withdrawn')),
    confirmed_at TEXT,
    UNIQUE(revision_id, party_id)
);

CREATE INDEX IF NOT EXISTS idx_commitments_party
ON party_commitments(revision_id, party_id);

CREATE TABLE IF NOT EXISTS funding_windows (
    funding_window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    window_code TEXT NOT NULL,
    window_type TEXT NOT NULL,
    opens_on TEXT NOT NULL,
    closes_on TEXT,
    amount TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    UNIQUE(commitment_id, window_code)
);

CREATE TABLE IF NOT EXISTS commitment_conditions (
    condition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    condition_code TEXT NOT NULL,
    condition_type TEXT NOT NULL,
    label TEXT NOT NULL,
    gate_amount TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','satisfied','waived','failed')),
    decided_by TEXT REFERENCES capital_users(user_id),
    decided_at TEXT,
    note TEXT,
    UNIQUE(commitment_id, condition_code)
);

CREATE TABLE IF NOT EXISTS condition_blocked_windows (
    condition_id INTEGER NOT NULL REFERENCES commitment_conditions(condition_id),
    funding_window_id INTEGER NOT NULL REFERENCES funding_windows(funding_window_id),
    PRIMARY KEY(condition_id, funding_window_id)
);

CREATE TABLE IF NOT EXISTS follow_on_rights (
    follow_on_right_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    right_code TEXT NOT NULL,
    future_round_code TEXT NOT NULL,
    pro_rata_percent TEXT NOT NULL,
    exercise_window_days INTEGER,
    UNIQUE(commitment_id, right_code)
);

CREATE TABLE IF NOT EXISTS use_restrictions (
    restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    restriction_code TEXT NOT NULL,
    restriction_type TEXT NOT NULL,
    allowed_categories_json TEXT NOT NULL DEFAULT '[]',
    notice_required INTEGER NOT NULL DEFAULT 0 CHECK(notice_required IN (0,1)),
    note TEXT,
    UNIQUE(commitment_id, restriction_code)
);

CREATE TABLE IF NOT EXISTS party_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    party_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('commitment','window','condition','right','restriction')),
    item_ref TEXT,
    confirmed_by TEXT NOT NULL REFERENCES capital_users(user_id),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, party_id, scope, item_ref)
);

CREATE TABLE IF NOT EXISTS capital_calls (
    call_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    use_category TEXT,
    due_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','partial','settled','cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    issued_by TEXT NOT NULL REFERENCES capital_users(user_id),
    issued_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS capital_call_items (
    call_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES capital_calls(call_id),
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    funding_window_id INTEGER REFERENCES funding_windows(funding_window_id),
    requested_amount TEXT NOT NULL,
    callable_amount TEXT NOT NULL,
    blocked_amount TEXT NOT NULL,
    received_amount TEXT NOT NULL DEFAULT '0',
    frozen_amount TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL CHECK(state IN ('blocked','callable','partial','settled','frozen')),
    block_reasons_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(call_id, commitment_id, funding_window_id)
);

CREATE INDEX IF NOT EXISTS idx_call_items_commitment
ON capital_call_items(commitment_id);

CREATE TABLE IF NOT EXISTS cash_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    call_item_id INTEGER REFERENCES capital_call_items(call_item_id),
    funding_window_id INTEGER REFERENCES funding_windows(funding_window_id),
    party_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('contribution','return')),
    return_kind TEXT CHECK(return_kind IN ('distribution','refund','milestone_return')),
    amount TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    use_category TEXT,
    basis_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'booked' CHECK(state IN ('booked','reversed')),
    idempotency_key TEXT NOT NULL UNIQUE,
    recorded_by TEXT NOT NULL REFERENCES capital_users(user_id),
    created_at TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_cash_project_party
ON cash_events(project_id, party_id, occurred_on, event_id);

CREATE TABLE IF NOT EXISTS capital_disputes (
    dispute_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision_id INTEGER NOT NULL REFERENCES agreement_revisions(revision_id),
    commitment_id INTEGER NOT NULL REFERENCES party_commitments(commitment_id),
    party_id TEXT NOT NULL,
    funding_window_id INTEGER REFERENCES funding_windows(funding_window_id),
    amount TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved','cancelled')),
    raised_by TEXT NOT NULL REFERENCES capital_users(user_id),
    raised_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_disputes_open
ON capital_disputes(project_id, state, dispute_id);

CREATE TABLE IF NOT EXISTS dispute_freezes (
    dispute_id TEXT NOT NULL REFERENCES capital_disputes(dispute_id),
    call_item_id INTEGER NOT NULL REFERENCES capital_call_items(call_item_id),
    frozen_amount TEXT NOT NULL,
    PRIMARY KEY(dispute_id, call_item_id)
);

CREATE TABLE IF NOT EXISTS capital_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS capital_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capital_audit_entity
ON capital_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


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
