from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from nemotron.staff.adapters.sqlite_runtime import SQLiteRuntimeStore
from nemotron.staff.domain.runtime import RuntimeError as StaffRuntimeError, WorkItem, WorkStatus


class SQLiteWorkerQueue:
    """Worker-safe queue adapter with atomic claims, retries, releases, and blocking."""

    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.store.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS runtime_worker_attempts (
                    work_item_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )"""
            )

    def claim_next(self, staff_id: str, *, at: datetime) -> WorkItem | None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM runtime_work_queue WHERE assigned_staff_id = ? AND status = ? ORDER BY created_at, work_item_id",
                (staff_id, WorkStatus.QUEUED.value),
            ).fetchall()
            chosen = None
            for row in rows:
                dependencies = tuple(json.loads(row["depends_on_json"]))
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    unresolved = db.execute(
                        f"SELECT COUNT(*) AS n FROM runtime_work_queue "
                        f"WHERE proposal_id = ? AND step_id IN ({placeholders}) AND status != ?",
                        (row["proposal_id"], *dependencies, WorkStatus.COMPLETED.value),
                    ).fetchone()["n"]
                    if unresolved:
                        continue
                chosen = row
                break
            if chosen is None:
                db.execute("COMMIT")
                return None

            result = db.execute(
                """UPDATE runtime_work_queue
                SET status = ?, claimed_at = ?, version = version + 1
                WHERE work_item_id = ? AND status = ? AND version = ?""",
                (
                    WorkStatus.CLAIMED.value,
                    at.isoformat(),
                    chosen["work_item_id"],
                    WorkStatus.QUEUED.value,
                    chosen["version"],
                ),
            )
            if result.rowcount != 1:
                raise StaffRuntimeError("Worker claim lost an optimistic-concurrency race.")

            db.execute(
                """INSERT INTO runtime_worker_attempts(work_item_id, attempts, last_error)
                VALUES (?, 1, NULL)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    attempts = runtime_worker_attempts.attempts + 1,
                    last_error = NULL""",
                (chosen["work_item_id"],),
            )
            row = db.execute(
                "SELECT * FROM runtime_work_queue WHERE work_item_id = ?",
                (chosen["work_item_id"],),
            ).fetchone()
            db.execute("COMMIT")
            return SQLiteRuntimeStore._work_from_row(row)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def complete(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        summary: str,
    ) -> WorkItem:
        return self.store.complete(
            work_item_id,
            staff_id=staff_id,
            expected_version=expected_version,
            at=at,
            summary=summary,
        )

    def release(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise StaffRuntimeError("Worker release reason cannot be empty.")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE runtime_work_queue
                SET status = ?, claimed_at = NULL, version = version + 1
                WHERE work_item_id = ? AND assigned_staff_id = ? AND status = ? AND version = ?""",
                (
                    WorkStatus.QUEUED.value,
                    work_item_id,
                    staff_id,
                    WorkStatus.CLAIMED.value,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise StaffRuntimeError("Worker release was stale, unauthorized, or not claimed.")
            db.execute(
                "UPDATE runtime_worker_attempts SET last_error = ? WHERE work_item_id = ?",
                (reason, work_item_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def block(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise StaffRuntimeError("Worker block reason cannot be empty.")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE runtime_work_queue
                SET status = ?, completed_at = ?, result_summary = ?, version = version + 1
                WHERE work_item_id = ? AND assigned_staff_id = ? AND status = ? AND version = ?""",
                (
                    WorkStatus.BLOCKED.value,
                    at.isoformat(),
                    reason,
                    work_item_id,
                    staff_id,
                    WorkStatus.CLAIMED.value,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise StaffRuntimeError("Worker block was stale, unauthorized, or not claimed.")
            db.execute(
                "UPDATE runtime_worker_attempts SET last_error = ? WHERE work_item_id = ?",
                (reason, work_item_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def attempts(self, work_item_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts FROM runtime_worker_attempts WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        return int(row["attempts"]) if row is not None else 0
