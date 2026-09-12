from __future__ import annotations

from datetime import datetime, timezone

from nemotron.staff.adapters.nemotron_worker import NemotronWorkerReasoningAdapter
from nemotron.staff.adapters.sqlite_core import SQLiteTaskRepository
from nemotron.staff.adapters.sqlite_runtime import (
    SQLiteGoalRepository,
    SQLiteMemoryRepository,
    SQLitePlanRepository,
    SQLiteRuntimeStore,
)
from nemotron.staff.adapters.sqlite_worker import SQLiteWorkerQueue
from nemotron.staff.application.memory import ReadVisibleMemory
from nemotron.staff.application.ports import AuditEvent, GovernanceAuditEvent
from nemotron.staff.application.worker import StaffWorkerEngine, WorkerRunStatus
from nemotron.staff.application.worker_ports import (
    WorkerAnalysis,
    WorkerAnalysisStatus,
    WorkerContext,
)
from nemotron.staff.domain import (
    Decision,
    Department,
    Evidence,
    GovernancePolicy,
    Organization,
    Permission,
    RiskLevel,
    Role,
    StaffMember,
    StaffPlacement,
    Task,
    TaskState,
)
from nemotron.staff.domain.runtime import (
    Goal,
    MemoryEntry,
    MemoryScope,
    PlanProposal,
    PlanStep,
    RuntimeError as StaffRuntimeError,
    WorkItem,
)


NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


class InMemoryStaff:
    def __init__(self, *members: StaffMember) -> None:
        self.items = {member.staff_id: member for member in members}

    def get(self, staff_id: str) -> StaffMember:
        try:
            return self.items[staff_id]
        except KeyError as exc:
            raise LookupError(staff_id) from exc

    def save(self, member: StaffMember) -> None:
        self.items[member.staff_id] = member

    def list_all(self) -> tuple[StaffMember, ...]:
        return tuple(self.items.values())


class InMemoryOrganizations:
    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    def get(self, organization_id: str) -> Organization:
        if organization_id != self.organization.organization_id:
            raise LookupError(organization_id)
        return self.organization

    def save(self, organization: Organization) -> None:
        self.organization = organization


class FixedClock:
    def now(self) -> datetime:
        return NOW


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent | GovernanceAuditEvent] = []

    def append(self, event: AuditEvent | GovernanceAuditEvent) -> None:
        self.events.append(event)


class ReadyReasoner:
    def __init__(self, memory_id: str = "mem-1") -> None:
        self.memory_id = memory_id
        self.calls = 0

    def analyze(self, context: WorkerContext) -> WorkerAnalysis:
        self.calls += 1
        return WorkerAnalysis(
            status=WorkerAnalysisStatus.READY,
            work_summary="Evidence and rationale handed to Staff Core.",
            evidence_memory_ids=(self.memory_id,),
            decision_rationale="The selected trusted evidence supports the assigned action.",
        )


class FailingReasoner:
    def analyze(self, context: WorkerContext) -> WorkerAnalysis:
        raise StaffRuntimeError("model unavailable")


class MustNotRunReasoner:
    def analyze(self, context: WorkerContext) -> WorkerAnalysis:
        raise AssertionError("Reasoner must not run for an already-decided governed task.")


def _worker() -> StaffMember:
    return StaffMember(
        "worker",
        "Operations Worker",
        Role(
            "worker-role",
            "Operations Worker",
            (
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
                Permission("read", "crm", RiskLevel.LOW),
                Permission("write", "erp", RiskLevel.HIGH),
            ),
        ),
    )


def _organization(worker: StaffMember) -> tuple[Organization, InMemoryStaff, InMemoryOrganizations]:
    organization = Organization(
        organization_id="org-1",
        name="MyNemotron Staff",
        departments=(Department("ops", "Operations"),),
        placements=(StaffPlacement(worker.staff_id, "ops", "Operator"),),
    )
    staff = InMemoryStaff(worker)
    return organization, staff, InMemoryOrganizations(organization)


def _build_runtime(tmp_path, *, risk: RiskLevel, source_reference: str | None, reasoner, max_attempts: int = 3):
    worker = _worker()
    organization, staff, organizations = _organization(worker)
    db_path = tmp_path / "staff.db"
    store = SQLiteRuntimeStore(db_path)
    memories_repo = SQLiteMemoryRepository(store)
    goals = SQLiteGoalRepository(store)
    plans = SQLitePlanRepository(store)
    queue = SQLiteWorkerQueue(store)
    tasks = SQLiteTaskRepository(db_path)
    audit = InMemoryAudit()

    goal = Goal("goal-1", "org-1", "Do governed work", "Process the assigned work safely.", "owner", NOW)
    goals.save(goal)
    memory = MemoryEntry(
        "mem-1",
        "org-1",
        "worker",
        MemoryScope.ORGANIZATION,
        "Trusted business fact from the source system.",
        NOW,
        source_reference=source_reference,
    )
    memories_repo.save(memory)

    action = "read" if risk is RiskLevel.LOW else "write"
    resource = "crm" if risk is RiskLevel.LOW else "erp"
    proposal = PlanProposal(
        "plan-1",
        goal.goal_id,
        "org-1",
        "owner",
        "One governed step",
        (PlanStep("s1", "Process work", action, resource, risk),),
        NOW,
    )
    plans.save(proposal)
    item = WorkItem(
        "work-1",
        "org-1",
        goal.goal_id,
        proposal.proposal_id,
        "s1",
        "Process work",
        action,
        resource,
        risk,
        "worker",
        NOW,
    )
    plans.accept_with_work_items(proposal.accept(NOW), expected_version=1, work_items=(item,))

    engine = StaffWorkerEngine(
        queue=queue,
        tasks=tasks,
        staff=staff,
        organizations=organizations,
        goals=goals,
        plans=plans,
        memories=ReadVisibleMemory(memories_repo, staff, organizations),
        reasoner=reasoner,
        policy=GovernancePolicy.conservative(),
        clock=FixedClock(),
        audit=audit,
        max_attempts=max_attempts,
    )
    return engine, queue, tasks, audit


def test_low_risk_worker_hands_decision_to_staff_core_without_execution(tmp_path) -> None:
    reasoner = ReadyReasoner()
    engine, queue, tasks, audit = _build_runtime(
        tmp_path,
        risk=RiskLevel.LOW,
        source_reference="crm:customer-42",
        reasoner=reasoner,
    )

    result = engine.run_once("worker")

    assert result.status is WorkerRunStatus.HANDOFF_READY
    assert result.task_state is TaskState.READY_FOR_EXECUTION
    task = tasks.get("work-task:work-1")
    assert task.state is TaskState.READY_FOR_EXECUTION
    assert task.execution_reference is None
    assert task.approval is None
    assert task.verification is None
    assert task.evidence[0].reference == "crm:customer-42"
    assert reasoner.calls == 1
    assert queue.attempts("work-1") == 1
    assert any(event.event_type == "worker.decision_handoff" for event in audit.events)


def test_high_risk_worker_stops_at_approval_gate(tmp_path) -> None:
    engine, _, tasks, _ = _build_runtime(
        tmp_path,
        risk=RiskLevel.HIGH,
        source_reference="erp:record-44",
        reasoner=ReadyReasoner(),
    )

    result = engine.run_once("worker")

    assert result.status is WorkerRunStatus.HANDOFF_READY
    assert result.task_state is TaskState.AWAITING_APPROVAL
    task = tasks.get("work-task:work-1")
    assert task.state is TaskState.AWAITING_APPROVAL
    assert task.execution_reference is None
    assert task.approval is None


def test_high_risk_worker_blocks_unsourced_memory(tmp_path) -> None:
    engine, _, tasks, audit = _build_runtime(
        tmp_path,
        risk=RiskLevel.HIGH,
        source_reference=None,
        reasoner=ReadyReasoner(),
    )

    result = engine.run_once("worker")

    assert result.status is WorkerRunStatus.BLOCKED
    assert tasks.get("work-task:work-1").state is TaskState.EVIDENCE
    assert any(event.event_type == "worker.work_blocked" for event in audit.events)
    assert engine.run_once("worker").status is WorkerRunStatus.IDLE


def test_reasoner_failure_releases_then_blocks_after_retry_limit(tmp_path) -> None:
    engine, queue, _, _ = _build_runtime(
        tmp_path,
        risk=RiskLevel.LOW,
        source_reference="crm:customer-42",
        reasoner=FailingReasoner(),
        max_attempts=2,
    )

    first = engine.run_once("worker")
    assert first.status is WorkerRunStatus.RETRY
    assert queue.attempts("work-1") == 1

    second = engine.run_once("worker")
    assert second.status is WorkerRunStatus.BLOCKED
    assert queue.attempts("work-1") == 2
    assert engine.run_once("worker").status is WorkerRunStatus.IDLE


def test_recovery_completes_work_without_repeating_existing_decision(tmp_path) -> None:
    engine, _, tasks, _ = _build_runtime(
        tmp_path,
        risk=RiskLevel.LOW,
        source_reference="crm:customer-42",
        reasoner=MustNotRunReasoner(),
    )
    task = Task(
        task_id="work-task:work-1",
        title="Process work",
        action="read",
        resource="crm",
        risk=RiskLevel.LOW,
        created_by="accepted-plan:plan-1",
        created_at=NOW,
    ).assign_to("worker")
    task = task.add_evidence(Evidence("staff-memory:organization", "crm:customer-42", "Fact", NOW))
    task = task.record_decision(Decision("read", "Already decided", NOW), approval_required=False)
    tasks.save(task)

    result = engine.run_once("worker")

    assert result.status is WorkerRunStatus.HANDOFF_READY
    assert result.task_state is TaskState.READY_FOR_EXECUTION


def test_worker_parser_rejects_execution_authority_and_invisible_memory(tmp_path) -> None:
    engine, _, tasks, _ = _build_runtime(
        tmp_path,
        risk=RiskLevel.LOW,
        source_reference="crm:customer-42",
        reasoner=ReadyReasoner(),
    )
    task = Task(
        task_id="work-task:work-1",
        title="Process work",
        action="read",
        resource="crm",
        risk=RiskLevel.LOW,
        created_by="accepted-plan:plan-1",
        created_at=NOW,
    ).assign_to("worker")
    goal = Goal("goal-1", "org-1", "Do governed work", "Process safely", "owner", NOW)
    memory = MemoryEntry("mem-1", "org-1", "worker", MemoryScope.ORGANIZATION, "Fact", NOW, source_reference="crm:42")
    work = WorkItem("work-1", "org-1", "goal-1", "plan-1", "s1", "Process work", "read", "crm", RiskLevel.LOW, "worker", NOW)
    context = WorkerContext("org-1", "worker", "Operations Worker", goal, work, task, (memory,))

    malicious = """{
      "status":"ready",
      "work_summary":"bad",
      "evidence_memory_ids":["mem-1"],
      "decision_rationale":"do it",
      "block_reason":null,
      "execute":true
    }"""
    try:
        NemotronWorkerReasoningAdapter.parse_content(malicious, context)
        raised = False
    except StaffRuntimeError:
        raised = True
    assert raised

    invisible = """{
      "status":"ready",
      "work_summary":"bad",
      "evidence_memory_ids":["secret-memory"],
      "decision_rationale":"do it",
      "block_reason":null
    }"""
    try:
        NemotronWorkerReasoningAdapter.parse_content(invisible, context)
        raised = False
    except StaffRuntimeError:
        raised = True
    assert raised
