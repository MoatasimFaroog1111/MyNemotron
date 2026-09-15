from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from nemotron.staff.domain.durable import (
    DurableRuntimeError,
    ExecutionRun,
    ExecutionStatus,
    RetryPolicy,
    RuntimeLimits,
)


class SQLiteExecutionRepository:
    """Atomic SQLite execution leases with retry scheduling and crash recovery."""

    def __init__(
        self,
        path: str | Path,
        *,
        limits: RuntimeLimits | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.path = str(path)
        self.limits = limits or RuntimeLimits()
        self.retry_policy = retry_policy or RetryPolicy()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_execution_runs (
                    run_id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_execution_status
                    ON runtime_execution_runs(status, created_at, run_id);
                CREATE INDEX IF NOT EXISTS idx_runtime_execution_work_item
                    ON runtime_execution_runs(work_item_id, created_at, run_id);
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ExecutionRun:
        return ExecutionRun(
            run_id=row["run_id"],
            work_item_id=row["work_item_id"],
            idempotency_key=row["idempotency_key"],
            status=ExecutionStatus(row["status"]),
            attempt=row["attempt"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]) if row["next_attempt_at"] else None,
            lease_owner=row["lease_owner"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]) if row["lease_expires_at"] else None,
            last_error=row["last_error"],
        )

    @staticmethod
    def _write(db: sqlite3.Connection, run: ExecutionRun) -> None:
        db.execute(
            """UPDATE runtime_execution_runs SET
                status = ?, attempt = ?, updated_at = ?, next_attempt_at = ?,
                lease_owner = ?, lease_expires_at = ?, last_error = ?
                WHERE run_id = ?""",
            (
                run.status.value,
                run.attempt,
                run.updated_at.isoformat(),
                run.next_attempt_at.isoformat() if run.next_attempt_at else None,
                run.lease_owner,
                run.lease_expires_at.isoformat() if run.lease_expires_at else None,
                run.last_error,
                run.run_id,
            ),
        )

    def submit(self, *, work_item_id: str, idempotency_key: str, at: datetime) -> ExecutionRun:
        if not work_item_id.strip() or not idempotency_key.strip():
            raise DurableRuntimeError("work_item_id and idempotency_key are required.")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM runtime_execution_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                run = self._from_row(existing)
                if run.work_item_id != work_item_id:
                    raise DurableRuntimeError("Idempotency key is already bound to another work item.")
                db.execute("COMMIT")
                return run
            run = ExecutionRun.pending(
                run_id=str(uuid4()),
                work_item_id=work_item_id,
                idempotency_key=idempotency_key,
                at=at,
            )
            db.execute(
                "INSERT INTO runtime_execution_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.work_item_id,
                    run.idempotency_key,
                    run.status.value,
                    run.attempt,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                    None,
                    None,
                    None,
                    None,
                ),
            )
            db.execute("COMMIT")
            return run
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def get(self, run_id: str) -> ExecutionRun:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runtime_execution_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise LookupError(run_id)
        return self._from_row(row)

    def list_runs(self) -> tuple[ExecutionRun, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_execution_runs ORDER BY created_at, run_id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def claim_next(self, *, worker_id: str, at: datetime) -> ExecutionRun | None:
        if not worker_id.strip():
            raise DurableRuntimeError("worker_id is required.")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM runtime_execution_runs ORDER BY created_at, run_id"
            ).fetchall()
            runs = tuple(self._from_row(row) for row in rows)
            active = sum(run.has_active_lease(at) for run in runs)
            if active >= self.limits.max_concurrency:
                db.execute("COMMIT")
                return None
            candidate = next((run for run in runs if run.is_claimable(at)), None)
            if candidate is None:
                db.execute("COMMIT")
                return None
            claimed = candidate.claim(
                worker_id=worker_id,
                at=at,
                lease_seconds=self.limits.lease_seconds,
            )
            self._write(db, claimed)
            db.execute("COMMIT")
            return claimed
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def heartbeat(self, run_id: str, *, worker_id: str, at: datetime) -> ExecutionRun:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runtime_execution_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise LookupError(run_id)
            updated = self._from_row(row).heartbeat(
                worker_id=worker_id,
                at=at,
                lease_seconds=self.limits.lease_seconds,
            )
            self._write(db, updated)
            db.execute("COMMIT")
            return updated
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def succeed(self, run_id: str, *, worker_id: str, at: datetime) -> ExecutionRun:
        return self._finish(run_id, worker_id=worker_id, at=at, error=None)

    def fail(self, run_id: str, *, worker_id: str, at: datetime, error: str) -> ExecutionRun:
        return self._finish(run_id, worker_id=worker_id, at=at, error=error)

    def _finish(
        self,
        run_id: str,
        *,
        worker_id: str,
        at: datetime,
        error: str | None,
    ) -> ExecutionRun:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runtime_execution_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise LookupError(run_id)
            current = self._from_row(row)
            updated = (
                current.succeed(worker_id=worker_id, at=at)
                if error is None
                else current.fail(
                    worker_id=worker_id,
                    at=at,
                    error=error,
                    retry_policy=self.retry_policy,
                )
            )
            self._write(db, updated)
            db.execute("COMMIT")
            return updated
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()
