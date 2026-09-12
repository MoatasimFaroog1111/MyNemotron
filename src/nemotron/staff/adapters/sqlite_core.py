from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from nemotron.staff.domain import (
    Approval,
    ApprovalOutcome,
    Decision,
    Evidence,
    RiskLevel,
    Task,
    TaskState,
    Verification,
)


class SQLiteTaskRepository:
    """Durable TaskRepository adapter for governed Staff Core tasks."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS staff_core_tasks (
                    task_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                )"""
            )

    @staticmethod
    def _serialize(task: Task) -> str:
        payload = {
            "task_id": task.task_id,
            "title": task.title,
            "action": task.action,
            "resource": task.resource,
            "risk": task.risk.value,
            "created_by": task.created_by,
            "created_at": task.created_at.isoformat(),
            "state": task.state.value,
            "assignee_id": task.assignee_id,
            "evidence": [
                {
                    "source": item.source,
                    "reference": item.reference,
                    "summary": item.summary,
                    "recorded_at": item.recorded_at.isoformat(),
                }
                for item in task.evidence
            ],
            "decision": (
                {
                    "action": task.decision.action,
                    "rationale": task.decision.rationale,
                    "decided_at": task.decision.decided_at.isoformat(),
                }
                if task.decision
                else None
            ),
            "approval": (
                {
                    "approver_id": task.approval.approver_id,
                    "outcome": task.approval.outcome.value,
                    "rationale": task.approval.rationale,
                    "decided_at": task.approval.decided_at.isoformat(),
                }
                if task.approval
                else None
            ),
            "execution_reference": task.execution_reference,
            "verification": (
                {
                    "verifier_id": task.verification.verifier_id,
                    "passed": task.verification.passed,
                    "summary": task.verification.summary,
                    "verified_at": task.verification.verified_at.isoformat(),
                }
                if task.verification
                else None
            ),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _deserialize(value: str) -> Task:
        payload = json.loads(value)
        decision_raw = payload.get("decision")
        approval_raw = payload.get("approval")
        verification_raw = payload.get("verification")
        return Task(
            task_id=payload["task_id"],
            title=payload["title"],
            action=payload["action"],
            resource=payload["resource"],
            risk=RiskLevel(payload["risk"]),
            created_by=payload["created_by"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            state=TaskState(payload["state"]),
            assignee_id=payload.get("assignee_id"),
            evidence=tuple(
                Evidence(
                    source=item["source"],
                    reference=item["reference"],
                    summary=item["summary"],
                    recorded_at=datetime.fromisoformat(item["recorded_at"]),
                )
                for item in payload.get("evidence", [])
            ),
            decision=(
                Decision(
                    action=decision_raw["action"],
                    rationale=decision_raw["rationale"],
                    decided_at=datetime.fromisoformat(decision_raw["decided_at"]),
                )
                if decision_raw
                else None
            ),
            approval=(
                Approval(
                    approver_id=approval_raw["approver_id"],
                    outcome=ApprovalOutcome(approval_raw["outcome"]),
                    rationale=approval_raw["rationale"],
                    decided_at=datetime.fromisoformat(approval_raw["decided_at"]),
                )
                if approval_raw
                else None
            ),
            execution_reference=payload.get("execution_reference"),
            verification=(
                Verification(
                    verifier_id=verification_raw["verifier_id"],
                    passed=bool(verification_raw["passed"]),
                    summary=verification_raw["summary"],
                    verified_at=datetime.fromisoformat(verification_raw["verified_at"]),
                )
                if verification_raw
                else None
            ),
        )

    def get(self, task_id: str) -> Task:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM staff_core_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise LookupError(task_id)
        return self._deserialize(row["payload_json"])

    def save(self, task: Task) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO staff_core_tasks(task_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET payload_json = excluded.payload_json""",
                (task.task_id, self._serialize(task)),
            )

    def list_all(self) -> tuple[Task, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM staff_core_tasks ORDER BY task_id").fetchall()
        return tuple(self._deserialize(row["payload_json"]) for row in rows)

    def list_by_state(self, *states: TaskState) -> tuple[Task, ...]:
        allowed = set(states)
        return tuple(task for task in self.list_all() if task.state in allowed)
