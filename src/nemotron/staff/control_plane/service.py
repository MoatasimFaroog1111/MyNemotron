from __future__ import annotations

import secrets
import threading
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from nemotron.staff.application.direct_instructions import SubmitDirectInstructionRequest
from nemotron.staff.application.ports import GovernanceAuditEvent
from nemotron.staff.application.tool_gateway import PrepareToolExecutionRequest
from nemotron.staff.domain import PermissionDenied
from nemotron.staff.domain.runtime import MemoryEntry, MemoryScope, RuntimeError as StaffRuntimeError
from nemotron.staff.workflows.bank_reconciliation import BankTransaction

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

    def review_bank_reconciliation(
        self,
        *,
        bank_statement_reference: str,
        bank_account_code: str,
        start_date: str,
        end_date: str,
        transactions: list[Mapping[str, Any]],
        actor_id: str = "control-plane",
    ) -> dict[str, Any]:
        """Persist normalized bank evidence and produce a read-only, draft reconciliation report."""
        reviewer = self.runtime.review_bank_reconciliation
        if reviewer is None:
            raise StaffRuntimeError(
                "Bank reconciliation is not configured; enable an approved reconciliation source first."
            )

        statement_reference = bank_statement_reference.strip()
        account_code = bank_account_code.strip()
        actor = actor_id.strip() or "control-plane"
        if not statement_reference:
            raise ValueError("bank_statement_reference is required.")
        if not account_code:
            raise ValueError("bank_account_code is required.")
        if not transactions:
            raise ValueError("At least one normalized bank transaction is required.")
        try:
            period_start = date.fromisoformat(start_date)
            period_end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("start_date and end_date must use ISO YYYY-MM-DD format.") from exc
        if period_end < period_start:
            raise ValueError("end_date cannot precede start_date.")

        normalized: list[BankTransaction] = []
        for index, row in enumerate(transactions, start=1):
            if not isinstance(row, Mapping):
                raise ValueError(f"transactions[{index}] must be a JSON object.")
            transaction_id = str(row.get("transaction_id", "")).strip()
            currency = str(row.get("currency", "")).strip()
            source_reference = str(row.get("source_reference", "")).strip()
            if not transaction_id or not currency or not source_reference:
                raise ValueError(
                    f"transactions[{index}] requires transaction_id, currency, and source_reference."
                )
            try:
                transaction_date = date.fromisoformat(str(row.get("transaction_date", "")))
            except ValueError as exc:
                raise ValueError(
                    f"transactions[{index}].transaction_date must use ISO YYYY-MM-DD format."
                ) from exc
            try:
                amount = Decimal(str(row.get("amount", "")))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"transactions[{index}].amount must be a decimal number.") from exc
            if not amount.is_finite():
                raise ValueError(f"transactions[{index}].amount must be finite.")
            partner_value = row.get("partner")
            partner = str(partner_value).strip() if partner_value is not None else None
            normalized.append(
                BankTransaction(
                    transaction_id=transaction_id,
                    transaction_date=transaction_date,
                    amount=amount,
                    currency=currency,
                    reference=str(row.get("reference", "")).strip(),
                    partner=partner or None,
                    source_reference=source_reference,
                )
            )

        persisted = tuple(normalized)
        self.runtime.bank_statements.replace_statement(statement_reference, persisted)
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "bank_reconciliation.statement_loaded",
                "bank_statement",
                statement_reference,
                actor,
                self.runtime.clock.now(),
                f"transactions={len(persisted)}; account={account_code}",
            )
        )

        report = reviewer(
            bank_statement_reference=statement_reference,
            bank_account_code=account_code,
            start_date=period_start,
            end_date=period_end,
        )
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "bank_reconciliation.review_generated",
                "bank_statement",
                statement_reference,
                actor,
                self.runtime.clock.now(),
                (
                    f"matched={report.matched_count}; review={report.review_count}; "
                    f"unmatched={report.unmatched_count}; ambiguous={report.ambiguous_count}; "
                    "draft_only=true"
                ),
            )
        )
        return report.to_review_dict()

    def approval_inbox(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.approval_inbox()]

    def execution_dashboard(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.queries.execution_dashboard()]

    def task(self, task_id: str) -> dict[str, Any]:
        return self.runtime.queries.task(task_id)

    def staff_directory(self) -> list[dict[str, Any]]:
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
            department = organization.department_for(staff_id)
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
        member = self.runtime.staff.get(staff_id)
        tasks = [
            self.runtime.queries.task_to_dict(task)
            for task in self.runtime.tasks.list_all()
            if task.assignee_id == staff_id
        ]
        queued = [
            {
                "work_item_id": item.work_item_id,
                "goal_id": item.goal_id,
                "title": item.title,
                "action": item.action,
                "resource": item.resource,
                "risk": item.risk.value,
                "status": item.status.value,
                "created_at": item.created_at.isoformat(),
                "result_summary": item.result_summary,
            }
            for item in self.runtime.worker_queue.inbox(staff_id)
        ]
        task_ids = {item["task_id"] for item in tasks}
        audit = []
        for item in self.runtime.audit.list_recent(limit=250):
            if item.actor_id == staff_id or (item.subject_type == "task" and item.subject_id in task_ids):
                audit.append({**asdict(item), "occurred_at": item.occurred_at.isoformat()})
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
                {"action": permission.action, "resource": permission.resource, "max_risk": permission.max_risk.value}
                for permission in member.role.permissions
            ],
            "placement": self._placement_for(staff_id),
            "queued_work": queued,
            "tasks": tasks,
            "audit": audit,
        }

    def _process_staff_instruction(self, staff_id: str, work_item_id: str) -> None:
        try:
            self.runtime.worker.run_until_idle(staff_id, max_items=20)
        except Exception as exc:
            self.runtime.audit.append(
                GovernanceAuditEvent(
                    "ui.instruction_background_failed",
                    "work_item",
                    work_item_id,
                    staff_id,
                    self.runtime.clock.now(),
                    f"background worker failed safely: {type(exc).__name__}",
                )
            )

    def submit_staff_instruction(self, staff_id: str, instruction: str) -> dict[str, Any]:
        item = self.runtime.submit_direct_instruction(
            SubmitDirectInstructionRequest(staff_id=staff_id, instruction=instruction)
        )

        memory = MemoryEntry(
            memory_id=self.runtime.ids.new_id(),
            organization_id=item.organization_id,
            owner_staff_id=staff_id,
            scope=MemoryScope.PRIVATE,
            content=instruction.strip(),
            created_at=self.runtime.clock.now(),
            source_reference=f"ui-instruction:{item.work_item_id}",
        )
        self.runtime.memories.save(memory)
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "ui.instruction_memory_recorded",
                "memory",
                memory.memory_id,
                "ui-operator",
                memory.created_at,
                f"work_item_id={item.work_item_id}; staff_id={staff_id}",
            )
        )

        expected_task_id = f"work-task:{item.work_item_id}"
        worker_thread = threading.Thread(
            target=self._process_staff_instruction,
            args=(staff_id, item.work_item_id),
            name=f"staff-instruction-{item.work_item_id[:10]}",
            daemon=True,
        )
        worker_thread.start()

        return {
            "accepted": True,
            "staff_id": item.assigned_staff_id,
            "work_item_id": item.work_item_id,
            "goal_id": item.goal_id,
            "status": "queued",
            "title": item.title,
            "execution": {
                "status": "processing",
                "task_id": expected_task_id,
                "task_state": None,
                "detail": "Instruction accepted; governed worker is processing asynchronously.",
            },
            "result": {"text": "تم استلام التعليمات وبدأ الموظف تنفيذها. ستظهر النتيجة تلقائيًا عند اكتمالها."},
        }

    def ui_overview(self) -> dict[str, Any]:
        return {
            "health": self.health(),
            "approvals": self.approval_inbox(),
            "executions": self.execution_dashboard(),
            "audit": self.audit_timeline(limit=80),
        }

    def audit_timeline(self, *, limit: int = 100, subject_type: str | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.runtime.queries.audit_timeline(limit=limit, subject_type=subject_type, subject_id=subject_id))

    def backups(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime.backups.list()]

    def create_backup(self, *, label: str = "manual", actor_id: str = "control-plane") -> dict[str, Any]:
        info = self.runtime.backups.create(label)
        self.runtime.audit.append(GovernanceAuditEvent("backup.created", "backup", info.name, actor_id, self.runtime.clock.now(), f"size_bytes={info.size_bytes}"))
        return asdict(info)

    def restore_backup(self, name: str, *, recovery_token: str, confirm: str, actor_id: str = "recovery-admin") -> dict[str, Any]:
        configured = self.runtime.config.recovery_token
        if not configured or not recovery_token or not secrets.compare_digest(configured, recovery_token):
            raise PermissionDenied("Recovery authorization failed.")
        if confirm != "RESTORE":
            raise ValueError("Backup restore requires confirm='RESTORE'.")
        info = self.runtime.backups.restore(name)
        self.runtime.audit.append(GovernanceAuditEvent("backup.restored", "backup", info.name, actor_id, self.runtime.clock.now(), "SQLite restore completed after integrity validation."))
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

    def prepare_tool_execution(self, task_id: str, *, actor_id: str, tool_id: str, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        intent = self.runtime.prepare_tool_execution(
            PrepareToolExecutionRequest(task_id=task_id, actor_id=actor_id, tool_id=tool_id, operation=operation, arguments=arguments)
        )
        return {
            "task_id": intent.task_id,
            "actor_id": intent.actor_id,
            "tool_id": intent.tool_id,
            "operation": intent.operation,
            "arguments_sha256": intent.arguments_sha256,
            "prepared_at": intent.prepared_at.isoformat(),
        }

    def decide_approval(self, task_id: str, *, approver_id: str, approved: bool, rationale: str) -> dict[str, Any]:
        task = self.runtime.approve_task(task_id, approver_id, approved=approved, rationale=rationale)
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

    def verify(self, task_id: str, *, verifier_id: str, passed: bool, summary: str) -> dict[str, Any]:
        task = self.runtime.verify_task(task_id, verifier_id, passed=passed, summary=summary)
        return self.runtime.queries.task_to_dict(task)
