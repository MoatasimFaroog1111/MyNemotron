from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from nemotron.staff.adapters.sqlite_control import SQLiteAuditLog
from nemotron.staff.adapters.sqlite_core import SQLiteTaskRepository
from nemotron.staff.adapters.sqlite_gateway import SQLiteIdempotencyRepository, SQLiteToolIntentRepository
from nemotron.staff.domain import Task, TaskState


@dataclass(frozen=True, slots=True)
class ApprovalInboxItem:
    task_id: str
    title: str
    action: str
    resource: str
    risk: str
    assignee_id: str | None
    evidence_count: int
    decision_rationale: str
    tool_id: str | None
    tool_operation: str | None
    arguments_sha256: str | None


@dataclass(frozen=True, slots=True)
class ExecutionDashboardItem:
    task_id: str
    title: str
    state: str
    action: str
    resource: str
    risk: str
    assignee_id: str | None
    approval_outcome: str | None
    execution_reference: str | None
    tool_id: str | None
    tool_operation: str | None
    idempotency_state: str | None


class ControlPlaneQueries:
    def __init__(
        self,
        tasks: SQLiteTaskRepository,
        intents: SQLiteToolIntentRepository,
        idempotency: SQLiteIdempotencyRepository,
        audit: SQLiteAuditLog,
    ) -> None:
        self._tasks = tasks
        self._intents = intents
        self._idempotency = idempotency
        self._audit = audit

    @staticmethod
    def _intent_for(intents: SQLiteToolIntentRepository, task_id: str):  # type: ignore[no-untyped-def]
        try:
            return intents.get(task_id)
        except LookupError:
            return None

    def approval_inbox(self) -> tuple[ApprovalInboxItem, ...]:
        items: list[ApprovalInboxItem] = []
        for task in self._tasks.list_by_state(TaskState.AWAITING_APPROVAL):
            intent = self._intent_for(self._intents, task.task_id)
            items.append(
                ApprovalInboxItem(
                    task_id=task.task_id,
                    title=task.title,
                    action=task.action,
                    resource=task.resource,
                    risk=task.risk.value,
                    assignee_id=task.assignee_id,
                    evidence_count=len(task.evidence),
                    decision_rationale=task.decision.rationale if task.decision else "",
                    tool_id=intent.tool_id if intent else None,
                    tool_operation=intent.operation if intent else None,
                    arguments_sha256=intent.arguments_sha256 if intent else None,
                )
            )
        return tuple(items)

    def execution_dashboard(self) -> tuple[ExecutionDashboardItem, ...]:
        interesting = {
            TaskState.READY_FOR_EXECUTION,
            TaskState.VERIFYING,
            TaskState.COMPLETED,
            TaskState.BLOCKED,
        }
        items: list[ExecutionDashboardItem] = []
        for task in self._tasks.list_all():
            if task.state not in interesting:
                continue
            intent = self._intent_for(self._intents, task.task_id)
            record = self._idempotency.get(task.execution_key)
            items.append(
                ExecutionDashboardItem(
                    task_id=task.task_id,
                    title=task.title,
                    state=task.state.value,
                    action=task.action,
                    resource=task.resource,
                    risk=task.risk.value,
                    assignee_id=task.assignee_id,
                    approval_outcome=task.approval.outcome.value if task.approval else None,
                    execution_reference=task.execution_reference,
                    tool_id=intent.tool_id if intent else None,
                    tool_operation=intent.operation if intent else None,
                    idempotency_state=record.state.value if record else None,
                )
            )
        return tuple(items)

    def task(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        return self.task_to_dict(task)

    @staticmethod
    def task_to_dict(task: Task) -> dict[str, Any]:
        return {
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

    def audit_timeline(
        self,
        *,
        limit: int = 100,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                **asdict(item),
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in self._audit.list_recent(
                limit=limit,
                subject_type=subject_type,
                subject_id=subject_id,
            )
        )
