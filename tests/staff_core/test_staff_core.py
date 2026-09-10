from datetime import datetime, timezone

import pytest

from nemotron.staff.application.ports import AuditEvent, ExecutionReceipt
from nemotron.staff.application.use_cases import (
    AddEvidence,
    ApproveTask,
    AssignTask,
    CreateTask,
    CreateTaskRequest,
    ExecuteTask,
    RecordDecision,
    VerifyTask,
)
from nemotron.staff.domain import (
    GovernancePolicy,
    InvalidTransition,
    Permission,
    PermissionDenied,
    RiskLevel,
    Role,
    StaffMember,
    Task,
    TaskState,
)


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


class InMemoryTasks:
    def __init__(self) -> None:
        self.items: dict[str, Task] = {}

    def get(self, task_id: str) -> Task:
        try:
            return self.items[task_id]
        except KeyError as exc:
            raise LookupError(task_id) from exc

    def save(self, task: Task) -> None:
        self.items[task.task_id] = task


class InMemoryStaff:
    def __init__(self, *members: StaffMember) -> None:
        self.items = {member.staff_id: member for member in members}

    def get(self, staff_id: str) -> StaffMember:
        try:
            return self.items[staff_id]
        except KeyError as exc:
            raise LookupError(staff_id) from exc


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedIds:
    def new_id(self) -> str:
        return "task-1"


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task: Task) -> ExecutionReceipt:
        self.calls += 1
        return ExecutionReceipt(reference="external-123", summary=f"Executed {task.action}")


def make_staff() -> tuple[StaffMember, StaffMember, StaffMember]:
    worker = StaffMember(
        "worker-1",
        "Operations Worker",
        Role(
            "worker",
            "Worker",
            (
                Permission("write", "erp", RiskLevel.HIGH),
                Permission("verify", "erp", RiskLevel.HIGH),
            ),
        ),
    )
    manager = StaffMember(
        "manager-1",
        "Operations Manager",
        Role(
            "manager",
            "Manager",
            (Permission("assign", "erp", RiskLevel.CRITICAL),),
            approval_limit=RiskLevel.CRITICAL,
        ),
    )
    auditor = StaffMember(
        "auditor-1",
        "Independent Auditor",
        Role(
            "auditor",
            "Auditor",
            (Permission("verify", "erp", RiskLevel.CRITICAL),),
        ),
    )
    return worker, manager, auditor


def build_high_risk_flow() -> tuple[InMemoryTasks, InMemoryStaff, InMemoryAudit, GovernancePolicy]:
    worker, manager, auditor = make_staff()
    tasks = InMemoryTasks()
    staff = InMemoryStaff(worker, manager, auditor)
    audit = InMemoryAudit()
    policy = GovernancePolicy.conservative()

    CreateTask(tasks, FixedIds(), FixedClock(), audit)(
        CreateTaskRequest(
            title="Update ERP record",
            action="write",
            resource="erp",
            risk=RiskLevel.HIGH,
            created_by="requester-1",
        )
    )
    AssignTask(tasks, staff, FixedClock(), audit)("task-1", "worker-1", "manager-1")
    AddEvidence(tasks, staff, FixedClock(), audit)(
        "task-1",
        "worker-1",
        source="erp",
        reference="record-44",
        summary="Source state and historical pattern validated",
    )
    RecordDecision(tasks, staff, policy, FixedClock(), audit)(
        "task-1",
        "worker-1",
        rationale="Evidence supports the controlled write",
    )
    return tasks, staff, audit, policy


def test_high_risk_task_cannot_execute_before_approval() -> None:
    tasks, staff, audit, _ = build_high_risk_flow()
    executor = FakeExecutor()

    with pytest.raises(InvalidTransition):
        ExecuteTask(tasks, staff, executor, FixedClock(), audit)("task-1", "worker-1")

    assert executor.calls == 0
    assert tasks.get("task-1").state is TaskState.AWAITING_APPROVAL


def test_self_approval_is_forbidden() -> None:
    tasks, staff, audit, _ = build_high_risk_flow()

    with pytest.raises(PermissionDenied, match="Self-approval"):
        ApproveTask(tasks, staff, FixedClock(), audit)(
            "task-1",
            "worker-1",
            approved=True,
            rationale="Approve my own work",
        )


def test_high_risk_execution_requires_independent_verification() -> None:
    tasks, staff, audit, policy = build_high_risk_flow()
    ApproveTask(tasks, staff, FixedClock(), audit)(
        "task-1",
        "manager-1",
        approved=True,
        rationale="Reviewed and approved",
    )

    executor = FakeExecutor()
    executed = ExecuteTask(tasks, staff, executor, FixedClock(), audit)("task-1", "worker-1")
    assert executed.state is TaskState.VERIFYING
    assert executor.calls == 1

    with pytest.raises(PermissionDenied, match="Independent verification"):
        VerifyTask(tasks, staff, policy, FixedClock(), audit)(
            "task-1",
            "worker-1",
            passed=True,
            summary="Self verification",
        )

    completed = VerifyTask(tasks, staff, policy, FixedClock(), audit)(
        "task-1",
        "auditor-1",
        passed=True,
        summary="Verified against external state",
    )
    assert completed.state is TaskState.COMPLETED
    assert [event.event_type for event in audit.events] == [
        "task.created",
        "task.assigned",
        "task.evidence_recorded",
        "task.decision_recorded",
        "task.approved",
        "task.executed",
        "task.verified",
    ]


def test_decision_requires_evidence() -> None:
    worker, manager, auditor = make_staff()
    tasks = InMemoryTasks()
    staff = InMemoryStaff(worker, manager, auditor)
    audit = InMemoryAudit()
    policy = GovernancePolicy.conservative()
    CreateTask(tasks, FixedIds(), FixedClock(), audit)(
        CreateTaskRequest("Write", "write", "erp", RiskLevel.HIGH, "requester-1")
    )
    AssignTask(tasks, staff, FixedClock(), audit)("task-1", "worker-1", "manager-1")

    with pytest.raises(InvalidTransition, match="evidence"):
        RecordDecision(tasks, staff, policy, FixedClock(), audit)(
            "task-1",
            "worker-1",
            rationale="No evidence",
        )
