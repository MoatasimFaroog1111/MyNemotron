from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.domain.model import (
    Approval,
    ApprovalOutcome,
    Decision,
    Evidence,
    GovernancePolicy,
    PermissionDenied,
    RiskLevel,
    Task,
    Verification,
)

from .ports import (
    ActionExecutorPort,
    AuditEvent,
    AuditPort,
    ClockPort,
    IdGeneratorPort,
    StaffRepository,
    TaskRepository,
)


@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    title: str
    action: str
    resource: str
    risk: RiskLevel
    created_by: str


class CreateTask:
    def __init__(self, tasks: TaskRepository, ids: IdGeneratorPort, clock: ClockPort, audit: AuditPort) -> None:
        self._tasks = tasks
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: CreateTaskRequest) -> Task:
        now = self._clock.now()
        task = Task(
            task_id=self._ids.new_id(),
            title=request.title,
            action=request.action,
            resource=request.resource,
            risk=request.risk,
            created_by=request.created_by,
            created_at=now,
        )
        self._tasks.save(task)
        self._audit.append(
            AuditEvent(
                event_type="task.created",
                task_id=task.task_id,
                actor_id=request.created_by,
                occurred_at=now,
                detail=f"{task.action} on {task.resource} at {task.risk.value} risk",
            )
        )
        return task


class AssignTask:
    def __init__(self, tasks: TaskRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._tasks = tasks
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, assignee_id: str, actor_id: str) -> Task:
        task = self._tasks.get(task_id)
        actor = self._staff.get(actor_id)
        assignee = self._staff.get(assignee_id)
        actor.assert_allowed("assign", task.resource, task.risk)
        assignee.assert_allowed(task.action, task.resource, task.risk)
        updated = task.assign_to(assignee.staff_id)
        self._tasks.save(updated)
        self._audit.append(
            AuditEvent("task.assigned", task_id, actor_id, self._clock.now(), f"assigned to {assignee_id}")
        )
        return updated


class AddEvidence:
    def __init__(self, tasks: TaskRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._tasks = tasks
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, actor_id: str, *, source: str, reference: str, summary: str) -> Task:
        task = self._tasks.get(task_id)
        actor = self._staff.get(actor_id)
        if task.assignee_id != actor_id:
            raise PermissionDenied("Only the assigned staff member may record evidence for this task.")
        actor.assert_allowed(task.action, task.resource, task.risk)
        now = self._clock.now()
        updated = task.add_evidence(Evidence(source, reference, summary, now))
        self._tasks.save(updated)
        self._audit.append(AuditEvent("task.evidence_recorded", task_id, actor_id, now, reference))
        return updated


class RecordDecision:
    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        policy: GovernancePolicy,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._policy = policy
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, actor_id: str, *, rationale: str) -> Task:
        task = self._tasks.get(task_id)
        actor = self._staff.get(actor_id)
        if task.assignee_id != actor_id:
            raise PermissionDenied("Only the assigned staff member may record the decision.")
        actor.assert_allowed(task.action, task.resource, task.risk)
        now = self._clock.now()
        updated = task.record_decision(
            Decision(task.action, rationale, now),
            approval_required=self._policy.requires_approval(task.risk),
        )
        self._tasks.save(updated)
        self._audit.append(AuditEvent("task.decision_recorded", task_id, actor_id, now, rationale))
        return updated


class ApproveTask:
    def __init__(self, tasks: TaskRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._tasks = tasks
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, approver_id: str, *, approved: bool, rationale: str) -> Task:
        task = self._tasks.get(task_id)
        approver = self._staff.get(approver_id)
        approver.assert_active()
        if task.assignee_id == approver_id:
            raise PermissionDenied("Self-approval is forbidden.")
        approver.assert_allowed("approve", task.resource, task.risk)
        if not approver.role.can_approve(task.risk):
            raise PermissionDenied(f"Role {approver.role.name!r} cannot approve {task.risk.value} risk tasks.")
        now = self._clock.now()
        updated = task.record_approval(
            Approval(
                approver_id,
                ApprovalOutcome.APPROVED if approved else ApprovalOutcome.REJECTED,
                rationale,
                now,
            )
        )
        self._tasks.save(updated)
        event_type = "task.approved" if approved else "task.rejected"
        self._audit.append(AuditEvent(event_type, task_id, approver_id, now, rationale))
        return updated


class ExecuteTask:
    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        executor: ActionExecutorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._executor = executor
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, actor_id: str) -> Task:
        task = self._tasks.get(task_id)
        actor = self._staff.get(actor_id)
        if task.assignee_id != actor_id:
            raise PermissionDenied("Only the assigned staff member may execute this task.")
        actor.assert_allowed(task.action, task.resource, task.risk)

        # Authorization gate is checked before the external side effect.
        task.assert_ready_for_execution()
        receipt = self._executor.execute(task, idempotency_key=task.execution_key)

        updated = task.record_execution(receipt.reference)
        self._tasks.save(updated)
        self._audit.append(
            AuditEvent(
                "task.executed",
                task_id,
                actor_id,
                self._clock.now(),
                f"{receipt.reference}: {receipt.summary}",
            )
        )
        return updated


class VerifyTask:
    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        policy: GovernancePolicy,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._policy = policy
        self._clock = clock
        self._audit = audit

    def __call__(self, task_id: str, verifier_id: str, *, passed: bool, summary: str) -> Task:
        task = self._tasks.get(task_id)
        verifier = self._staff.get(verifier_id)
        verifier.assert_allowed("verify", task.resource, task.risk)
        if self._policy.requires_independent_verification(task.risk) and task.assignee_id == verifier_id:
            raise PermissionDenied("Independent verification is required at this risk level.")
        now = self._clock.now()
        updated = task.record_verification(Verification(verifier_id, passed, summary, now))
        self._tasks.save(updated)
        event_type = "task.verified" if passed else "task.verification_failed"
        self._audit.append(AuditEvent(event_type, task_id, verifier_id, now, summary))
        return updated
