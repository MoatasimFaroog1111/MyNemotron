from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from nemotron.staff.adapters.sqlite_runtime import SQLiteRuntimeStore
from nemotron.staff.domain.durable import RetryPolicy, RuntimeLimits
from nemotron.staff.domain.runtime import RuntimeError as StaffRuntimeError, WorkItem, WorkStatus


class SQLiteWorkerQueue:
    """Worker-safe queue with leases, delayed retries, concurrency limits, and crash recovery."""

    def __init__(
        self,
        store: SQLiteRuntimeStore,
        *,
        limits: RuntimeLimits | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.store = store
        self.limits = limits or RuntimeLimits(max_concurrency=4, lease_seconds=300)
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=0,
            max_delay_seconds=0,
            backoff_factor=1.0,
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.store.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> frozenset[str]:
        return frozenset(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall())

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS runtime_worker_attempts (
                    work_item_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT
                )"""
            )
            columns = self._columns(db, "runtime_worker_attempts")
            if "lease_expires_at" not in columns:
                db.execute("ALTER TABLE runtime_worker_attempts ADD COLUMN lease_expires_at TEXT")
            if "next_attempt_at" not in columns:
                db.execute("ALTER TABLE runtime_worker_attempts ADD COLUMN next_attempt_at TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_worker_attempt_schedule ON runtime_worker_attempts(next_attempt_at, lease_expires_at)"
            )

    def enqueue(self, item: WorkItem) -> None:
        self.store.enqueue(item)

    def inbox(self, staff_id: str) -> tuple[WorkItem, ...]:
        return self.store.inbox(staff_id)

    def _recover_expired_leases(self, db: sqlite3.Connection, *, at: datetime) -> int:
        expired = db.execute(
            """SELECT q.work_item_id, COALESCE(a.attempts, 0) AS attempts
            FROM runtime_work_queue AS q
            LEFT JOIN runtime_worker_attempts AS a ON a.work_item_id = q.work_item_id
            WHERE q.status = ? AND (a.lease_expires_at IS NULL OR a.lease_expires_at <= ?)""",
            (WorkStatus.CLAIMED.value, at.isoformat()),
        ).fetchall()
        if not expired:
            return 0

        retry_ids = tuple(
            str(row["work_item_id"])
            for row in expired
            if int(row["attempts"]) < self.retry_policy.max_attempts
        )
        exhausted_ids = tuple(
            str(row["work_item_id"])
            for row in expired
            if int(row["attempts"]) >= self.retry_policy.max_attempts
        )

        if retry_ids:
            placeholders = ",".join("?" for _ in retry_ids)
            db.execute(
                f"""UPDATE runtime_work_queue
                SET status = ?, claimed_at = NULL, version = version + 1
                WHERE work_item_id IN ({placeholders}) AND status = ?""",
                (WorkStatus.QUEUED.value, *retry_ids, WorkStatus.CLAIMED.value),
            )
            db.execute(
                f"""UPDATE runtime_worker_attempts
                SET lease_expires_at = NULL,
                    last_error = 'worker lease expired; recovered for retry'
                WHERE work_item_id IN ({placeholders})""",
                retry_ids,
            )

        if exhausted_ids:
            placeholders = ",".join("?" for _ in exhausted_ids)
            summary = "Worker lease expired; retry limit reached; human review required."
            db.execute(
                f"""UPDATE runtime_work_queue
                SET status = ?, claimed_at = NULL, completed_at = ?, result_summary = ?, version = version + 1
                WHERE work_item_id IN ({placeholders}) AND status = ?""",
                (
                    WorkStatus.BLOCKED.value,
                    at.isoformat(),
                    summary,
                    *exhausted_ids,
                    WorkStatus.CLAIMED.value,
                ),
            )
            db.execute(
                f"""UPDATE runtime_worker_attempts
                SET lease_expires_at = NULL,
                    next_attempt_at = NULL,
                    last_error = 'worker lease expired; retry limit reached'
                WHERE work_item_id IN ({placeholders})""",
                exhausted_ids,
            )

        return len(expired)

    def _active_lease_count(self, db: sqlite3.Connection, *, at: datetime) -> int:
        row = db.execute(
            """SELECT COUNT(*) AS n
            FROM runtime_work_queue AS q
            JOIN runtime_worker_attempts AS a ON a.work_item_id = q.work_item_id
            WHERE q.status = ? AND a.lease_expires_at > ?""",
            (WorkStatus.CLAIMED.value, at.isoformat()),
        ).fetchone()
        return int(row["n"])

    def claim_next(self, staff_id: str, *, at: datetime) -> WorkItem | None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._recover_expired_leases(db, at=at)
            if self._active_lease_count(db, at=at) >= self.limits.max_concurrency:
                db.execute("COMMIT")
                return None

            rows = db.execute(
                """SELECT q.*, a.next_attempt_at
                FROM runtime_work_queue AS q
                LEFT JOIN runtime_worker_attempts AS a ON a.work_item_id = q.work_item_id
                WHERE q.assigned_staff_id = ? AND q.status = ?
                ORDER BY q.created_at, q.work_item_id""",
                (staff_id, WorkStatus.QUEUED.value),
            ).fetchall()
            chosen = None
            for row in rows:
                if row["next_attempt_at"] is not None and datetime.fromisoformat(row["next_attempt_at"]) > at:
                    continue
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

            lease_expires_at = at + timedelta(seconds=self.limits.lease_seconds)
            db.execute(
                """INSERT INTO runtime_worker_attempts(
                    work_item_id, attempts, last_error, lease_expires_at, next_attempt_at
                ) VALUES (?, 1, NULL, ?, NULL)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    attempts = runtime_worker_attempts.attempts + 1,
                    last_error = NULL,
                    lease_expires_at = excluded.lease_expires_at,
                    next_attempt_at = NULL""",
                (chosen["work_item_id"], lease_expires_at.isoformat()),
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

    def claim_work_item(self, work_item_id: str, *, staff_id: str, at: datetime) -> WorkItem | None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._recover_expired_leases(db, at=at)
            if self._active_lease_count(db, at=at) >= self.limits.max_concurrency:
                db.execute("COMMIT")
                return None

            row = db.execute(
                """SELECT q.*, a.next_attempt_at
                FROM runtime_work_queue AS q
                LEFT JOIN runtime_worker_attempts AS a ON a.work_item_id = q.work_item_id
                WHERE q.work_item_id = ? AND q.assigned_staff_id = ? AND q.status = ?""",
                (work_item_id, staff_id, WorkStatus.QUEUED.value),
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            if row["next_attempt_at"] is not None and datetime.fromisoformat(row["next_attempt_at"]) > at:
                db.execute("COMMIT")
                return None

            dependencies = tuple(json.loads(row["depends_on_json"]))
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                unresolved = db.execute(
                    f"SELECT COUNT(*) AS n FROM runtime_work_queue "
                    f"WHERE proposal_id = ? AND step_id IN ({placeholders}) AND status != ?",
                    (row["proposal_id"], *dependencies, WorkStatus.COMPLETED.value),
                ).fetchone()["n"]
                if unresolved:
                    db.execute("COMMIT")
                    return None

            result = db.execute(
                """UPDATE runtime_work_queue
                SET status = ?, claimed_at = ?, version = version + 1
                WHERE work_item_id = ? AND assigned_staff_id = ? AND status = ? AND version = ?""",
                (
                    WorkStatus.CLAIMED.value,
                    at.isoformat(),
                    work_item_id,
                    staff_id,
                    WorkStatus.QUEUED.value,
                    row["version"],
                ),
            )
            if result.rowcount != 1:
                raise StaffRuntimeError("Targeted worker claim lost an optimistic-concurrency race.")

            lease_expires_at = at + timedelta(seconds=self.limits.lease_seconds)
            db.execute(
                """INSERT INTO runtime_worker_attempts(
                    work_item_id, attempts, last_error, lease_expires_at, next_attempt_at
                ) VALUES (?, 1, NULL, ?, NULL)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    attempts = runtime_worker_attempts.attempts + 1,
                    last_error = NULL,
                    lease_expires_at = excluded.lease_expires_at,
                    next_attempt_at = NULL""",
                (work_item_id, lease_expires_at.isoformat()),
            )
            claimed = db.execute(
                "SELECT * FROM runtime_work_queue WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            db.execute("COMMIT")
            return SQLiteRuntimeStore._work_from_row(claimed)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def heartbeat(self, work_item_id: str, *, staff_id: str, at: datetime) -> None:
        lease_expires_at = at + timedelta(seconds=self.limits.lease_seconds)
        with self._connect() as db:
            result = db.execute(
                """UPDATE runtime_worker_attempts
                SET lease_expires_at = ?
                WHERE work_item_id = ?
                  AND EXISTS (
                    SELECT 1 FROM runtime_work_queue
                    WHERE work_item_id = ? AND assigned_staff_id = ? AND status = ?
                  )""",
                (
                    lease_expires_at.isoformat(),
                    work_item_id,
                    work_item_id,
                    staff_id,
                    WorkStatus.CLAIMED.value,
                ),
            )
            if result.rowcount != 1:
                raise StaffRuntimeError("Cannot heartbeat a work item without a live claimed lease.")

    def complete(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        summary: str,
    ) -> WorkItem:
        completed = self.store.complete(
            work_item_id,
            staff_id=staff_id,
            expected_version=expected_version,
            at=at,
            summary=summary,
        )
        with self._connect() as db:
            db.execute(
                "UPDATE runtime_worker_attempts SET lease_expires_at = NULL, next_attempt_at = NULL WHERE work_item_id = ?",
                (work_item_id,),
            )
        return completed

    def release(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        reason: str,
    ) -> None:
        self._requeue(
            work_item_id,
            staff_id=staff_id,
            expected_version=expected_version,
            reason=reason,
            next_attempt_at=None,
        )

    def schedule_retry(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        reason: str,
    ) -> datetime:
        attempt = self.attempts(work_item_id)
        if attempt < 1:
            raise StaffRuntimeError("Cannot schedule retry before the first worker attempt.")
        next_attempt_at = at + self.retry_policy.delay_for_attempt(attempt)
        self._requeue(
            work_item_id,
            staff_id=staff_id,
            expected_version=expected_version,
            reason=reason,
            next_attempt_at=next_attempt_at,
        )
        return next_attempt_at

    def _requeue(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        reason: str,
        next_attempt_at: datetime | None,
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
                """UPDATE runtime_worker_attempts
                SET last_error = ?, lease_expires_at = NULL, next_attempt_at = ?
                WHERE work_item_id = ?""",
                (reason, next_attempt_at.isoformat() if next_attempt_at else None, work_item_id),
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
                """UPDATE runtime_worker_attempts
                SET last_error = ?, lease_expires_at = NULL, next_attempt_at = NULL
                WHERE work_item_id = ?""",
                (reason, work_item_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def status(self, work_item_id: str) -> WorkStatus | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM runtime_work_queue WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkStatus(row["status"])

    def attempts(self, work_item_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts FROM runtime_worker_attempts WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def next_attempt_at(self, work_item_id: str) -> datetime | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT next_attempt_at FROM runtime_worker_attempts WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        if row is None or row["next_attempt_at"] is None:
            return None
        return datetime.fromisoformat(row["next_attempt_at"])
