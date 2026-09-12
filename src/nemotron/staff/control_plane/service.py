from __future__ import annotations

import secrets
from dataclasses import asdict
from typing import Any, Mapping

from nemotron.staff.application.ports import GovernanceAuditEvent
from nemotron.staff.application.tool_gateway import PrepareToolExecutionRequest
from nemotron.staff.domain import PermissionDenied

from .runtime import ProductionRuntime


class ControlPlaneService:
    """Application-facing control surface. HTTP and the browser UI are adapters over this service."""

    def __init__(self, runtime: ProductionRuntime) -> None:
        self.runtime = runtime

    def health(self) -> dict[str, object]:
        return self.runtime.health()

    def readiness(self, *, force: bool = False) -> dict[str, object]:
        return self.runtime.readiness(force=force)

    def config_summary(self) -> dict[str, object]:
        return self.runtime.config.redacted_summary()

    def approval_inbox(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.approval_inbox()]

    def execution_dashboard(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.execution_dashboard()]

    def task(self, task_id: str) -> dict[str, Any]:
        return self.runtime.queries.task(task_id)

    def staff_directory(self) -> list[dict[str, Any]]:
        """Return safe Staff Registry metadata for the official UI; never return secrets or tool handles."""
        return [
            {
                "staff_id": member.staff_id,
                "display_name": member.display_name,
                "status": member.status.value,
                "role_id": member.role.role_id,
                "role_name": member.role.name,
                "approval_limit": member.role.approval_limit.value if member.role.approval_limit else None,
            }
            for member in self.runtime.staff.list_all()
        ]

    def _placement_for(self, staff_id: str) -> dict[str, Any] | None:
        for organization in self.runtime.organizations.list_all():
            try:
                placement = organization.placement_for(staff_id)
            except LookupError:
                continue
            department = organization.department_for(placement.department_id)
            return {
                "organization_id": organization.organization_id,
                "organization_name": organization.name,
                "department_id": department.department_id,
                "department_name": department.name,
                "job_title": placement.job_title,
                "manager_id": placement.manager_id,
                "chief_of_staff": organization.chief_of_staff_id == staff_id,
            }
        return None

    def staff_workspace(self, staff_id: str) -> dict[str, Any]:
        """Read-only staff workspace projection used by the browser BFF surface."""
        member = self.runtime.staff.get(staff_id)
        tasks = [
            self.runtime.queries.task_to_dict(task)
            for task in self.runtime.tasks.list_all()
            if task.assignee_id == staff_id
        ]
        task_ids = {item["task_id"] for item in tasks}
        audit = []
        for item in self.runtime.audit.list_recent(limit=250):
            if item.actor_id == staff_id or (item.subject_type == "task" and item.subject_id in task_ids):
                audit.append(
                    {
                        **asdict(item),
                        "occurred_at": item.occurred_at.isoformat(),
                    }
                )
            if len(audit) >= 40:
                break
        return {
            "staff_id": member.staff_id,
            "display_name": member.display_name,
            "status": member.status.value,
            "role_id": member.role.role_id,
            "role_name": member.role.name,
            "approval_limit": member.role.approval_limit.value if member.role.approval_limit else None,
            "permissions": [
                {
                    "action": permission.action,
                    "resource": permission.resource,
                    "max_risk": permission.max_risk.value,
                }
                for permission in member.role.permissions
            ],
            "placement": self._placement_for(staff_id),
            "tasks": tasks,
            "audit": audit,
        }

    def ui_overview(self) -> dict[str, Any]:
        """Safe read-only operational summary for the official frontend."""
        return {
            "health": self.health(),
            "approvals": self.approval_inbox(),
            "executions": self.execution_dashboard(),
            "audit": self.audit_timeline(limit=80),
        }

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

    def backups(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.backups.list()]

    def create_backup(self, *, label: str = "manual", actor_id: str = "control-plane") -> dict[str, Any]:
        info = self.runtime.backups.create(label)
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "backup.created",
                "backup",
                info.name,
                actor_id,
                self.runtime.clock.now(),
                f"size_bytes={info.size_bytes}",
            )
        )
        return asdict(info)

    def restore_backup(
        self,
        name: str,
        *,
        recovery_token: str,
        confirm: str,
        actor_id: str = "recovery-admin",
    ) -> dict[str, Any]:
        configured = self.runtime.config.recovery_token
        if not configured or not recovery_token or not secrets.compare_digest(configured, recovery_token):
            raise PermissionDenied("Recovery authorization failed.")
        if confirm != "RESTORE":
            raise ValueError("Backup restore requires confirm='RESTORE'.")
        info = self.runtime.backups.restore(name)
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "backup.restored",
                "backup",
                info.name,
                actor_id,
                self.runtime.clock.now(),
                "SQLite restore completed after integrity validation.",
            )
        )
        return asdict(info)

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
        self.runtime.backups.create("pre-execution")
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
