from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from nemotron.staff.application.memory import ReadVisibleMemory
from nemotron.staff.application.ports import (
    AuditPort,
    ClockPort,
    GovernanceAuditEvent,
    OrganizationRepository,
    StaffRepository,
    TaskRepository,
)
from nemotron.staff.application.runtime_ports import GoalRepository, PlanRepository
from nemotron.staff.application.use_cases import AddEvidence, RecordDecision
from nemotron.staff.domain import GovernancePolicy, InvalidTransition, PermissionDenied, RiskLevel, Task, TaskState
from nemotron.staff.domain.runtime import GoalStatus, RuntimeError as StaffRuntimeError, WorkItem

from .worker_ports import (
    WorkerAnalysisStatus,
    WorkerContext,
    WorkerQueuePort,
    WorkerReasoningPort,
)


class WorkerRunStatus(str, Enum):
    IDLE = "idle"
    HANDOFF_READY = "handoff_ready"
    BLOCKED = "blocked"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    status: WorkerRunStatus
    staff_id: str
    work_item_id: str | None = None
    task_id: str | None = None
    task_state: TaskState | None = None
    detail: str = ""


class WorkerBlocked(StaffRuntimeError):
    """Raised when trusted runtime rules require human review instead of another retry."""


class MaterializeWorkTask:
    """Project an accepted, already-assigned WorkItem into the governed Task aggregate."""

    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        plans: PlanRepository,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._organizations = organizations
        self._plans = plans
        self._clock = clock
        self._audit = audit

    @staticmethod
    def task_id_for(item: WorkItem) -> str:
        return f"work-task:{item.work_item_id}"

    def __call__(self, item: WorkItem) -> Task:
        proposal = self._plans.get(item.proposal_id)
        if proposal.accepted_at is None:
            raise WorkerBlocked("Work item belongs to a plan that has not been accepted.")
        if proposal.organization_id != item.organization_id or proposal.goal_id != item.goal_id:
            raise WorkerBlocked("Work item does not match its accepted plan scope.")

        step = next((candidate for candidate in proposal.steps if candidate.step_id == item.step_id), None)
        if step is None:
            raise WorkerBlocked("Work item step is not present in the accepted plan.")
        if (
            step.title != item.title
            or step.action != item.action
            or step.resource != item.resource
            or step.risk is not item.risk
            or step.depends_on != item.depends_on
        ):
            raise WorkerBlocked("Work item authority differs from the accepted plan step.")

        member = self._staff.get(item.assigned_staff_id)
        member.assert_allowed(item.action, item.resource, item.risk)
        organization = self._organizations.get(item.organization_id)
        organization.placement_for(member.staff_id)

        task_id = self.task_id_for(item)
        try:
            existing = self._tasks.get(task_id)
        except LookupError:
            task = Task(
                task_id=task_id,
                title=item.title,
                action=item.action,
                resource=item.resource,
                risk=item.risk,
                created_by=f"accepted-plan:{item.proposal_id}",
                created_at=item.created_at,
            ).assign_to(member.staff_id)
            self._tasks.save(task)
            self._audit.append(
                GovernanceAuditEvent(
                    "worker.task_materialized",
                    "task",
                    task.task_id,
                    member.staff_id,
                    self._clock.now(),
                    f"from work item {item.work_item_id}",
                )
            )
            return task

        if (
            existing.assignee_id != member.staff_id
            or existing.action != item.action
            or existing.resource != item.resource
            or existing.risk is not item.risk
        ):
            raise WorkerBlocked("Existing governed task does not match the accepted work item.")
        return existing


class StaffWorkerEngine:
    """Claim, reason, and hand work into Staff Core without executing external actions."""

    def __init__(
        self,
        *,
        queue: WorkerQueuePort,
        tasks: TaskRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        goals: GoalRepository,
        plans: PlanRepository,
        memories: ReadVisibleMemory,
        reasoner: WorkerReasoningPort,
        policy: GovernancePolicy,
        clock: ClockPort,
        audit: AuditPort,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive.")
        self._queue = queue
        self._tasks = tasks
        self._staff = staff
        self._goals = goals
        self._memories = memories
        self._reasoner = reasoner
        self._clock = clock
        self._audit = audit
        self._max_attempts = max_attempts
        self._materialize = MaterializeWorkTask(tasks, staff, organizations, plans, clock, audit)
        self._add_evidence = AddEvidence(tasks, staff, clock, audit)
        self._record_decision = RecordDecision(tasks, staff, policy, clock, audit)

    def run_once(self, staff_id: str) -> WorkerRunResult:
        member = self._staff.get(staff_id)
        member.assert_active()
        item = self._queue.claim_next(staff_id, at=self._clock.now())
        if item is None:
            return WorkerRunResult(WorkerRunStatus.IDLE, staff_id, detail="No eligible work.")

        try:
            member.assert_allowed(item.action, item.resource, item.risk)
            self._audit.append(
                GovernanceAuditEvent(
                    "worker.work_claimed",
                    "work_item",
                    item.work_item_id,
                    staff_id,
                    self._clock.now(),
                    item.title,
                )
            )
            return self._process_claimed(item, member.role.name)
        except WorkerBlocked as exc:
            reason = str(exc)
            self._queue.block(
                item.work_item_id,
                staff_id=staff_id,
                expected_version=item.version,
                at=self._clock.now(),
                reason=reason,
            )
            self._audit.append(
                GovernanceAuditEvent(
                    "worker.work_blocked",
                    "work_item",
                    item.work_item_id,
                    staff_id,
                    self._clock.now(),
                    reason,
                )
            )
            return WorkerRunResult(
                WorkerRunStatus.BLOCKED,
                staff_id,
                item.work_item_id,
                self._materialize.task_id_for(item),
                detail=reason,
            )
        except (PermissionDenied, InvalidTransition) as exc:
            reason = f"governance rejected worker processing: {type(exc).__name__}"
            self._queue.block(
                item.work_item_id,
                staff_id=staff_id,
                expected_version=item.version,
                at=self._clock.now(),
                reason=reason,
            )
            self._audit.append(
                GovernanceAuditEvent(
                    "worker.governance_blocked",
                    "work_item",
                    item.work_item_id,
                    staff_id,
                    self._clock.now(),
                    reason,
                )
            )
            return WorkerRunResult(
                WorkerRunStatus.BLOCKED,
                staff_id,
                item.work_item_id,
                self._materialize.task_id_for(item),
                detail=reason,
            )
        except (StaffRuntimeError, TimeoutError) as exc:
            return self._handle_retryable(item, type(exc).__name__)
        except Exception as exc:
            self._release(item, f"unexpected:{type(exc).__name__}")
            raise

    def _process_claimed(self, item: WorkItem, role_name: str) -> WorkerRunResult:
        task = self._materialize(item)
        if task.state is not TaskState.EVIDENCE:
            if task.decision is None:
                raise WorkerBlocked(f"Governed task is already {task.state.value} without a reusable decision.")
            completed = self._queue.complete(
                item.work_item_id,
                staff_id=item.assigned_staff_id,
                expected_version=item.version,
                at=self._clock.now(),
                summary=f"Governed task {task.task_id} already advanced to {task.state.value}.",
            )
            return WorkerRunResult(
                WorkerRunStatus.HANDOFF_READY,
                item.assigned_staff_id,
                completed.work_item_id,
                task.task_id,
                task.state,
                "Recovered an already-created decision without re-running the model.",
            )

        goal = self._goals.get(item.goal_id)
        if goal.organization_id != item.organization_id:
            raise WorkerBlocked("Work item goal is outside its organization.")
        if goal.status is not GoalStatus.ACTIVE:
            raise WorkerBlocked(f"Goal is {goal.status.value}; new reasoning is not allowed.")

        visible_memory = self._memories(item.organization_id, item.assigned_staff_id)
        context = WorkerContext(
            organization_id=item.organization_id,
            staff_id=item.assigned_staff_id,
            role_name=role_name,
            goal=goal,
            work_item=item,
            task=task,
            visible_memory=visible_memory,
        )
        analysis = self._reasoner.analyze(context)
        if analysis.status is WorkerAnalysisStatus.BLOCKED:
            raise WorkerBlocked("Reasoner reported insufficient or conflicting evidence.")

        memory_by_id = {entry.memory_id: entry for entry in visible_memory}
        unknown = set(analysis.evidence_memory_ids) - set(memory_by_id)
        if unknown:
            raise WorkerBlocked("Reasoner referenced memory that is not visible to this worker.")
        selected = tuple(memory_by_id[memory_id] for memory_id in analysis.evidence_memory_ids)
        if not selected:
            raise WorkerBlocked("No trusted evidence was selected for the decision.")

        if item.risk.severity >= RiskLevel.HIGH.severity:
            unsourced = [entry.memory_id for entry in selected if not (entry.source_reference or "").strip()]
            if unsourced:
                raise WorkerBlocked("High-risk decisions require source-referenced evidence.")

        existing_references = {evidence.reference for evidence in task.evidence}
        for entry in selected:
            reference = entry.source_reference or f"memory:{entry.memory_id}"
            if reference in existing_references:
                continue
            self._add_evidence(
                task.task_id,
                item.assigned_staff_id,
                source=f"staff-memory:{entry.scope.value}",
                reference=reference,
                summary=entry.content,
            )
            existing_references.add(reference)

        decided = self._record_decision(
            task.task_id,
            item.assigned_staff_id,
            rationale=analysis.decision_rationale or "",
        )
        safe_summary = f"Governed decision handed to Staff Core task {decided.task_id}; state={decided.state.value}."
        completed = self._queue.complete(
            item.work_item_id,
            staff_id=item.assigned_staff_id,
            expected_version=item.version,
            at=self._clock.now(),
            summary=safe_summary,
        )
        self._audit.append(
            GovernanceAuditEvent(
                "worker.decision_handoff",
                "task",
                decided.task_id,
                item.assigned_staff_id,
                self._clock.now(),
                f"work={completed.work_item_id}; state={decided.state.value}",
            )
        )
        return WorkerRunResult(
            WorkerRunStatus.HANDOFF_READY,
            item.assigned_staff_id,
            completed.work_item_id,
            decided.task_id,
            decided.state,
            safe_summary,
        )

    def _handle_retryable(self, item: WorkItem, error_type: str) -> WorkerRunResult:
        attempts = self._queue.attempts(item.work_item_id)
        reason = f"worker processing failed safely: {error_type}"
        if attempts >= self._max_attempts:
            self._queue.block(
                item.work_item_id,
                staff_id=item.assigned_staff_id,
                expected_version=item.version,
                at=self._clock.now(),
                reason=f"{reason}; retry limit reached",
            )
            self._audit.append(
                GovernanceAuditEvent(
                    "worker.retry_exhausted",
                    "work_item",
                    item.work_item_id,
                    item.assigned_staff_id,
                    self._clock.now(),
                    reason,
                )
            )
            return WorkerRunResult(
                WorkerRunStatus.BLOCKED,
                item.assigned_staff_id,
                item.work_item_id,
                self._materialize.task_id_for(item),
                detail="Retry limit reached; human review required.",
            )
        self._release(item, reason)
        return WorkerRunResult(
            WorkerRunStatus.RETRY,
            item.assigned_staff_id,
            item.work_item_id,
            self._materialize.task_id_for(item),
            detail=reason,
        )

    def _release(self, item: WorkItem, reason: str) -> None:
        self._queue.release(
            item.work_item_id,
            staff_id=item.assigned_staff_id,
            expected_version=item.version,
            reason=reason,
        )
        self._audit.append(
            GovernanceAuditEvent(
                "worker.work_released",
                "work_item",
                item.work_item_id,
                item.assigned_staff_id,
                self._clock.now(),
                reason,
            )
        )

    def run_until_idle(self, staff_id: str, *, max_items: int = 100) -> tuple[WorkerRunResult, ...]:
        if max_items < 1:
            raise ValueError("max_items must be positive.")
        results: list[WorkerRunResult] = []
        for _ in range(max_items):
            result = self.run_once(staff_id)
            results.append(result)
            if result.status in {WorkerRunStatus.IDLE, WorkerRunStatus.RETRY}:
                break
        return tuple(results)
