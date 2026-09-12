from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from nemotron.staff.application.tool_gateway import PrepareToolExecutionRequest

from .runtime import ProductionRuntime


class ControlPlaneService:
    """Application-facing control surface. HTTP is only an adapter over this service."""

    def __init__(self, runtime: ProductionRuntime) -> None:
        self.runtime = runtime

    def health(self) -> dict[str, object]:
        return self.runtime.health()

    def config_summary(self) -> dict[str, object]:
        return self.runtime.config.redacted_summary()

    def approval_inbox(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.approval_inbox()]

    def execution_dashboard(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.execution_dashboard()]

    def task(self, task_id: str) -> dict[str, Any]:
        return self.runtime.queries.task(task_id)

    def audit_timeline(
        self,
        *,
        limit: int = 100,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return list(
            self.runtime.queries.audit_timeline(
                limit=limit,
                subject_type=subject_type,
                subject_id=subject_id,
            )
        )

    def run_worker(self, staff_id: str) -> dict[str, Any]:
        result = self.runtime.worker.run_once(staff_id)
        return {
            "status": result.status.value,
            "staff_id": result.staff_id,
            "work_item_id": result.work_item_id,
            "task_id": result.task_id,
            "task_state": result.task_state.value if result.task_state else None,
            "detail": result.detail,
        }

    def prepare_tool_execution(
        self,
        task_id: str,
        *,
        actor_id: str,
        tool_id: str,
        operation: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        intent = self.runtime.prepare_tool_execution(
            PrepareToolExecutionRequest(
                task_id=task_id,
                actor_id=actor_id,
                tool_id=tool_id,
                operation=operation,
                arguments=arguments,
            )
        )
        return {
            "task_id": intent.task_id,
            "actor_id": intent.actor_id,
            "tool_id": intent.tool_id,
            "operation": intent.operation,
            "arguments_sha256": intent.arguments_sha256,
            "prepared_at": intent.prepared_at.isoformat(),
        }

    def decide_approval(
        self,
        task_id: str,
        *,
        approver_id: str,
        approved: bool,
        rationale: str,
    ) -> dict[str, Any]:
        task = self.runtime.approve_task(
            task_id,
            approver_id,
            approved=approved,
            rationale=rationale,
        )
        return self.runtime.queries.task_to_dict(task)

    def execute(self, task_id: str, *, actor_id: str) -> dict[str, Any]:
        result = self.runtime.execute_tool_task(task_id, actor_id)
        return {
            "task": self.runtime.queries.task_to_dict(result.task),
            "receipt": {
                "tool_id": result.receipt.tool_id,
                "operation": result.receipt.operation,
                "reference": result.receipt.reference,
                "summary": result.receipt.summary,
                "output": result.receipt.output(),
            },
            "recovered": result.recovered,
        }

    def verify(
        self,
        task_id: str,
        *,
        verifier_id: str,
        passed: bool,
        summary: str,
    ) -> dict[str, Any]:
        task = self.runtime.verify_task(
            task_id,
            verifier_id,
            passed=passed,
            summary=summary,
        )
        return self.runtime.queries.task_to_dict(task)
