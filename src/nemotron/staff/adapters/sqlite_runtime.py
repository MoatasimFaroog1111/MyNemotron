from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.runtime import Goal, GoalStatus, MemoryEntry, MemoryScope, PlanProposal, PlanStep, RuntimeError, WorkItem, WorkStatus


class SQLiteRuntimeStore:
    """SQLite adapter for durable runtime memory, goals, plans, and work queue state."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_memory (
                    memory_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    owner_staff_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    department_id TEXT,
                    source_reference TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_memory_org ON runtime_memory(organization_id, created_at);

                CREATE TABLE IF NOT EXISTS runtime_goals (
                    goal_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    owner_staff_id TEXT,
                    department_id TEXT,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_goals_active ON runtime_goals(organization_id, status, created_at);

                CREATE TABLE IF NOT EXISTS runtime_plans (
                    proposal_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    accepted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS runtime_work_queue (
                    work_item_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    assigned_staff_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT,
                    result_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_work_inbox
                    ON runtime_work_queue(assigned_staff_id, status, created_at, work_item_id);
                """
            )

    # MemoryRepository
    def save_memory(self, entry: MemoryEntry) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runtime_memory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.memory_id,
                    entry.organization_id,
                    entry.owner_staff_id,
                    entry.scope.value,
                    entry.content,
                    entry.created_at.isoformat(),
                    entry.department_id,
                    entry.source_reference,
                ),
            )

    def list_for_organization(self, organization_id: str) -> tuple[MemoryEntry, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_memory WHERE organization_id = ? ORDER BY created_at, memory_id",
                (organization_id,),
            ).fetchall()
        return tuple(
            MemoryEntry(
                memory_id=row["memory_id"],
                organization_id=row["organization_id"],
                owner_staff_id=row["owner_staff_id"],
                scope=MemoryScope(row["scope"]),
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
                department_id=row["department_id"],
                source_reference=row["source_reference"],
            )
            for row in rows
        )

    # GoalRepository
    def save_goal(self, goal: Goal) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO runtime_goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    owner_staff_id=excluded.owner_staff_id, department_id=excluded.department_id,
                    status=excluded.status""",
                (
                    goal.goal_id,
                    goal.organization_id,
                    goal.title,
                    goal.description,
                    goal.created_by,
                    goal.created_at.isoformat(),
                    goal.owner_staff_id,
                    goal.department_id,
                    goal.status.value,
                ),
            )

    def get_goal(self, goal_id: str) -> Goal:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runtime_goals WHERE goal_id = ?", (goal_id,)).fetchone()
        if row is None:
            raise LookupError(goal_id)
        return Goal(
            goal_id=row["goal_id"],
            organization_id=row["organization_id"],
            title=row["title"],
            description=row["description"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            owner_staff_id=row["owner_staff_id"],
            department_id=row["department_id"],
            status=GoalStatus(row["status"]),
        )

    def list_active(self, organization_id: str) -> tuple[Goal, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT goal_id FROM runtime_goals WHERE organization_id = ? AND status = ? ORDER BY created_at, goal_id",
                (organization_id, GoalStatus.ACTIVE.value),
            ).fetchall()
        return tuple(self.get_goal(row["goal_id"]) for row in rows)

    # PlanRepository
    @staticmethod
    def _steps_to_json(steps: tuple[PlanStep, ...]) -> str:
        return json.dumps(
            [
                {
                    "step_id": step.step_id,
                    "title": step.title,
                    "action": step.action,
                    "resource": step.resource,
                    "risk": step.risk.value,
                    "department_id": step.department_id,
                    "depends_on": list(step.depends_on),
                }
                for step in steps
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _steps_from_json(value: str) -> tuple[PlanStep, ...]:
        return tuple(
            PlanStep(
                step_id=item["step_id"],
                title=item["title"],
                action=item["action"],
                resource=item["resource"],
                risk=RiskLevel(item["risk"]),
                department_id=item.get("department_id"),
                depends_on=tuple(item.get("depends_on", [])),
            )
            for item in json.loads(value)
        )

    def save_plan(self, proposal: PlanProposal) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO runtime_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    summary=excluded.summary, steps_json=excluded.steps_json,
                    version=excluded.version, accepted_at=excluded.accepted_at""",
                (
                    proposal.proposal_id,
                    proposal.goal_id,
                    proposal.organization_id,
                    proposal.requested_by,
                    proposal.summary,
                    self._steps_to_json(proposal.steps),
                    proposal.created_at.isoformat(),
                    proposal.version,
                    proposal.accepted_at.isoformat() if proposal.accepted_at else None,
                ),
            )

    def get_plan(self, proposal_id: str) -> PlanProposal:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runtime_plans WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise LookupError(proposal_id)
        return PlanProposal(
            proposal_id=row["proposal_id"],
            goal_id=row["goal_id"],
            organization_id=row["organization_id"],
            requested_by=row["requested_by"],
            summary=row["summary"],
            steps=self._steps_from_json(row["steps_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            version=row["version"],
            accepted_at=datetime.fromisoformat(row["accepted_at"]) if row["accepted_at"] else None,
        )

    def accept_with_work_items(self, proposal: PlanProposal, *, expected_version: int, work_items: tuple[WorkItem, ...]) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version, accepted_at FROM runtime_plans WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchone()
            if row is None:
                raise LookupError(proposal.proposal_id)
            if row["version"] != expected_version or row["accepted_at"] is not None:
                raise RuntimeError("Plan proposal changed or was already accepted.")
            result = db.execute(
                "UPDATE runtime_plans SET version = ?, accepted_at = ? WHERE proposal_id = ? AND version = ? AND accepted_at IS NULL",
                (proposal.version, proposal.accepted_at.isoformat() if proposal.accepted_at else None, proposal.proposal_id, expected_version),
            )
            if result.rowcount != 1:
                raise RuntimeError("Plan acceptance lost an optimistic-concurrency race.")
            for item in work_items:
                self._insert_work(db, item)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # WorkQueueRepository
    def _insert_work(self, db: sqlite3.Connection, item: WorkItem) -> None:
        db.execute(
            """INSERT INTO runtime_work_queue (
                work_item_id, organization_id, goal_id, proposal_id, step_id, title,
                action, resource, risk, assigned_staff_id, created_at, depends_on_json,
                status, version, claimed_at, completed_at, result_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.work_item_id,
                item.organization_id,
                item.goal_id,
                item.proposal_id,
                item.step_id,
                item.title,
                item.action,
                item.resource,
                item.risk.value,
                item.assigned_staff_id,
                item.created_at.isoformat(),
                json.dumps(list(item.depends_on), separators=(",", ":")),
                item.status.value,
                item.version,
                item.claimed_at.isoformat() if item.claimed_at else None,
                item.completed_at.isoformat() if item.completed_at else None,
                item.result_summary,
            ),
        )

    def enqueue(self, item: WorkItem) -> None:
        with self._connect() as db:
            self._insert_work(db, item)

    @staticmethod
    def _work_from_row(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            work_item_id=row["work_item_id"],
            organization_id=row["organization_id"],
            goal_id=row["goal_id"],
            proposal_id=row["proposal_id"],
            step_id=row["step_id"],
            title=row["title"],
            action=row["action"],
            resource=row["resource"],
            risk=RiskLevel(row["risk"]),
            assigned_staff_id=row["assigned_staff_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            depends_on=tuple(json.loads(row["depends_on_json"])),
            status=WorkStatus(row["status"]),
            version=row["version"],
            claimed_at=datetime.fromisoformat(row["claimed_at"]) if row["claimed_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            result_summary=row["result_summary"],
        )

    def inbox(self, staff_id: str) -> tuple[WorkItem, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_work_queue WHERE assigned_staff_id = ? AND status IN (?, ?) ORDER BY created_at, work_item_id",
                (staff_id, WorkStatus.QUEUED.value, WorkStatus.CLAIMED.value),
            ).fetchall()
        return tuple(self._work_from_row(row) for row in rows)

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
                        f"SELECT COUNT(*) AS n FROM runtime_work_queue WHERE proposal_id = ? AND step_id IN ({placeholders}) AND status != ?",
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
                "UPDATE runtime_work_queue SET status = ?, claimed_at = ?, version = version + 1 WHERE work_item_id = ? AND status = ? AND version = ?",
                (WorkStatus.CLAIMED.value, at.isoformat(), chosen["work_item_id"], WorkStatus.QUEUED.value, chosen["version"]),
            )
            if result.rowcount != 1:
                raise RuntimeError("Work item claim lost an optimistic-concurrency race.")
            row = db.execute("SELECT * FROM runtime_work_queue WHERE work_item_id = ?", (chosen["work_item_id"],)).fetchone()
            db.execute("COMMIT")
            return self._work_from_row(row)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def complete(self, work_item_id: str, *, staff_id: str, expected_version: int, at: datetime, summary: str) -> WorkItem:
        if not summary.strip():
            raise RuntimeError("Work result summary cannot be empty.")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE runtime_work_queue
                SET status = ?, completed_at = ?, result_summary = ?, version = version + 1
                WHERE work_item_id = ? AND assigned_staff_id = ? AND status = ? AND version = ?""",
                (
                    WorkStatus.COMPLETED.value,
                    at.isoformat(),
                    summary,
                    work_item_id,
                    staff_id,
                    WorkStatus.CLAIMED.value,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("Work completion was stale, unauthorized, or not in claimed state.")
            row = db.execute("SELECT * FROM runtime_work_queue WHERE work_item_id = ?", (work_item_id,)).fetchone()
            db.execute("COMMIT")
            return self._work_from_row(row)
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()


class SQLiteMemoryRepository:
    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store

    def save(self, entry: MemoryEntry) -> None:
        self.store.save_memory(entry)

    def list_for_organization(self, organization_id: str) -> tuple[MemoryEntry, ...]:
        return self.store.list_for_organization(organization_id)


class SQLiteGoalRepository:
    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store

    def get(self, goal_id: str) -> Goal:
        return self.store.get_goal(goal_id)

    def save(self, goal: Goal) -> None:
        self.store.save_goal(goal)

    def list_active(self, organization_id: str) -> tuple[Goal, ...]:
        return self.store.list_active(organization_id)


class SQLitePlanRepository:
    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store

    def get(self, proposal_id: str) -> PlanProposal:
        return self.store.get_plan(proposal_id)

    def save(self, proposal: PlanProposal) -> None:
        self.store.save_plan(proposal)

    def accept_with_work_items(self, proposal: PlanProposal, *, expected_version: int, work_items: tuple[WorkItem, ...]) -> None:
        self.store.accept_with_work_items(proposal, expected_version=expected_version, work_items=work_items)


class SQLiteWorkQueueRepository:
    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store

    def enqueue(self, item: WorkItem) -> None:
        self.store.enqueue(item)

    def inbox(self, staff_id: str) -> tuple[WorkItem, ...]:
        return self.store.inbox(staff_id)

    def claim_next(self, staff_id: str, *, at: datetime) -> WorkItem | None:
        return self.store.claim_next(staff_id, at=at)

    def complete(self, work_item_id: str, *, staff_id: str, expected_version: int, at: datetime, summary: str) -> WorkItem:
        return self.store.complete(work_item_id, staff_id=staff_id, expected_version=expected_version, at=at, summary=summary)
