from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from nemotron.staff.domain.tools import (
    IdempotencyRecord,
    IdempotencyState,
    ToolExecutionIntent,
    ToolExecutionReceipt,
    ToolGatewayError,
)


class SQLiteGatewayStore:
    """Durable execution-intent and idempotency ledger for the tool gateway."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_execution_intents (
                    task_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    canonical_arguments_json TEXT NOT NULL,
                    arguments_sha256 TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_idempotency (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tool_id TEXT,
                    operation TEXT,
                    receipt_reference TEXT,
                    receipt_summary TEXT,
                    output_json TEXT
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tool_idempotency)").fetchall()}
            if "output_json" not in columns:
                db.execute("ALTER TABLE tool_idempotency ADD COLUMN output_json TEXT")


class SQLiteToolIntentRepository:
    def __init__(self, store: SQLiteGatewayStore) -> None:
        self._store = store

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ToolExecutionIntent:
        return ToolExecutionIntent(
            task_id=row["task_id"],
            actor_id=row["actor_id"],
            tool_id=row["tool_id"],
            operation=row["operation"],
            canonical_arguments_json=row["canonical_arguments_json"],
            arguments_sha256=row["arguments_sha256"],
            prepared_at=datetime.fromisoformat(row["prepared_at"]),
        )

    def get(self, task_id: str) -> ToolExecutionIntent:
        with self._store._connect() as db:
            row = db.execute("SELECT * FROM tool_execution_intents WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise LookupError(task_id)
        return self._from_row(row)

    def save_if_absent(self, intent: ToolExecutionIntent) -> ToolExecutionIntent:
        db = self._store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tool_execution_intents WHERE task_id = ?", (intent.task_id,)).fetchone()
            if row is not None:
                existing = self._from_row(row)
                if existing.fingerprint != intent.fingerprint:
                    raise ToolGatewayError("Task already has a different immutable tool execution intent.")
                db.execute("COMMIT")
                return existing
            db.execute(
                """INSERT INTO tool_execution_intents (
                    task_id, actor_id, tool_id, operation, canonical_arguments_json,
                    arguments_sha256, prepared_at, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    intent.task_id,
                    intent.actor_id,
                    intent.tool_id,
                    intent.operation,
                    intent.canonical_arguments_json,
                    intent.arguments_sha256,
                    intent.prepared_at.isoformat(),
                    intent.fingerprint,
                ),
            )
            db.execute("COMMIT")
            return intent
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()


class SQLiteIdempotencyRepository:
    def __init__(self, store: SQLiteGatewayStore) -> None:
        self._store = store

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IdempotencyRecord:
        receipt = None
        if row["state"] == IdempotencyState.COMPLETED.value:
            receipt = ToolExecutionReceipt(
                tool_id=row["tool_id"],
                operation=row["operation"],
                reference=row["receipt_reference"],
                summary=row["receipt_summary"],
                output_json=row["output_json"],
            )
        return IdempotencyRecord(
            key=row["key"],
            fingerprint=row["fingerprint"],
            state=IdempotencyState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            receipt=receipt,
        )

    def get(self, key: str) -> IdempotencyRecord | None:
        with self._store._connect() as db:
            row = db.execute("SELECT * FROM tool_idempotency WHERE key = ?", (key,)).fetchone()
        return None if row is None else self._from_row(row)

    def reserve(self, key: str, fingerprint: str, *, at: datetime) -> IdempotencyRecord:
        db = self._store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tool_idempotency WHERE key = ?", (key,)).fetchone()
            if row is not None:
                record = self._from_row(row)
                if record.fingerprint != fingerprint:
                    raise ToolGatewayError("Idempotency key is already bound to another execution intent.")
                raise ToolGatewayError(f"Idempotency key is already {record.state.value}; automatic duplicate blocked.")
            db.execute(
                "INSERT INTO tool_idempotency (key, fingerprint, state, created_at) VALUES (?, ?, ?, ?)",
                (key, fingerprint, IdempotencyState.PROCESSING.value, at.isoformat()),
            )
            db.execute("COMMIT")
            return IdempotencyRecord(key, fingerprint, IdempotencyState.PROCESSING, at)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def complete(self, key: str, fingerprint: str, receipt: ToolExecutionReceipt) -> IdempotencyRecord:
        db = self._store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE tool_idempotency
                SET state = ?, tool_id = ?, operation = ?, receipt_reference = ?, receipt_summary = ?, output_json = ?
                WHERE key = ? AND fingerprint = ? AND state = ?""",
                (
                    IdempotencyState.COMPLETED.value,
                    receipt.tool_id,
                    receipt.operation,
                    receipt.reference,
                    receipt.summary,
                    receipt.output_json,
                    key,
                    fingerprint,
                    IdempotencyState.PROCESSING.value,
                ),
            )
            if result.rowcount != 1:
                raise ToolGatewayError("Idempotency completion lost state or fingerprint ownership.")
            row = db.execute("SELECT * FROM tool_idempotency WHERE key = ?", (key,)).fetchone()
            db.execute("COMMIT")
            if row is None:
                raise ToolGatewayError("Completed idempotency record disappeared.")
            return self._from_row(row)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def release_safe_failure(self, key: str, fingerprint: str) -> None:
        with self._store._connect() as db:
            db.execute(
                "DELETE FROM tool_idempotency WHERE key = ? AND fingerprint = ? AND state = ?",
                (key, fingerprint, IdempotencyState.PROCESSING.value),
            )
