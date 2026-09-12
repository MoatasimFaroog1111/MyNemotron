from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nemotron.staff.adapters.nemotron_planner import NemotronPlanningAdapter
from nemotron.staff.adapters.sqlite_runtime import (
    SQLiteGoalRepository,
    SQLiteMemoryRepository,
    SQLitePlanRepository,
    SQLiteRuntimeStore,
    SQLiteWorkQueueRepository,
)
from nemotron.staff.application.memory import ReadVisibleMemory
from nemotron.staff.application.runtime_ports import PlanningContext
from nemotron.staff.domain import Department, Organization, Permission, RiskLevel, Role, StaffMember, StaffPlacement
from nemotron.staff.domain.runtime import Goal, MemoryEntry, MemoryScope, PlanProposal, PlanStep, RuntimeError, WorkItem, WorkStatus


NOW = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


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


def _member(staff_id: str) -> StaffMember:
    return StaffMember(
        staff_id,
        staff_id,
        Role(
            role_id=f"role-{staff_id}",
            name="Staff",
            permissions=(
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
                Permission("memory.write", "staff-memory", RiskLevel.MEDIUM),
                Permission("plan.propose", "staff-planning", RiskLevel.MEDIUM),
                Permission("plan.accept", "staff-planning", RiskLevel.HIGH),
                Permission("write", "erp", RiskLevel.HIGH),
                Permission("read", "crm", RiskLevel.LOW),
            ),
        ),
    )


def _organization() -> tuple[Organization, InMemoryStaff]:
    chief = _member("chief")
    worker = _member("worker")
    outsider = _member("outsider")
    organization = Organization(
        organization_id="org-1",
        name="MyNemotron Staff",
        departments=(Department("ops", "Operations"), Department("sales", "Sales")),
        placements=(
            StaffPlacement("chief", "ops", "Chief of Staff"),
            StaffPlacement("worker", "ops", "Operator", "chief"),
            StaffPlacement("outsider", "sales", "Sales Agent", "chief"),
        ),
        chief_of_staff_id="chief",
    )
    return organization, InMemoryStaff(chief, worker, outsider)


def test_sqlite_runtime_persists_memory_goal_plan_and_work(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    memories = SQLiteMemoryRepository(store)
    goals = SQLiteGoalRepository(store)
    plans = SQLitePlanRepository(store)
    queue = SQLiteWorkQueueRepository(store)

    memory = MemoryEntry("mem-1", "org-1", "chief", MemoryScope.ORGANIZATION, "Customer needs a quote", NOW)
    memories.save(memory)
    assert memories.list_for_organization("org-1") == (memory,)

    goal = Goal("goal-1", "org-1", "Prepare quote", "Prepare and review a quote", "chief", NOW)
    goals.save(goal)
    assert goals.get("goal-1") == goal

    proposal = PlanProposal(
        proposal_id="plan-1",
        goal_id=goal.goal_id,
        organization_id="org-1",
        requested_by="chief",
        summary="Prepare quote safely",
        steps=(PlanStep("s1", "Collect CRM facts", "read", "crm", RiskLevel.LOW),),
        created_at=NOW,
    )
    plans.save(proposal)
    work = WorkItem("work-1", "org-1", "goal-1", "plan-1", "s1", "Collect CRM facts", "read", "crm", RiskLevel.LOW, "worker", NOW)
    accepted = proposal.accept(NOW)
    plans.accept_with_work_items(accepted, expected_version=1, work_items=(work,))

    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    reopened_queue = SQLiteWorkQueueRepository(reopened)
    assert reopened.get_plan("plan-1").accepted_at == NOW
    assert reopened_queue.inbox("worker")[0].work_item_id == "work-1"


def test_work_queue_claim_is_single_and_completion_is_versioned(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    queue = SQLiteWorkQueueRepository(store)
    item = WorkItem("work-1", "org-1", "goal-1", "plan-1", "s1", "Read CRM", "read", "crm", RiskLevel.LOW, "worker", NOW)
    queue.enqueue(item)

    first = queue.claim_next("worker", at=NOW)
    assert first is not None
    assert first.status is WorkStatus.CLAIMED
    assert first.version == 2
    assert queue.claim_next("worker", at=NOW) is None

    with pytest.raises(RuntimeError, match="stale|Stale|unauthorized|claimed"):
        queue.complete("work-1", staff_id="worker", expected_version=1, at=NOW, summary="done")

    completed = queue.complete("work-1", staff_id="worker", expected_version=2, at=NOW, summary="done")
    assert completed.status is WorkStatus.COMPLETED
    assert completed.version == 3


def test_dependencies_block_claim_until_prior_step_completes(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    queue = SQLiteWorkQueueRepository(store)
    first = WorkItem("w1", "org-1", "g1", "p1", "s1", "First", "read", "crm", RiskLevel.LOW, "worker", NOW)
    second = WorkItem("w2", "org-1", "g1", "p1", "s2", "Second", "read", "crm", RiskLevel.LOW, "worker", NOW, depends_on=("s1",))
    queue.enqueue(first)
    queue.enqueue(second)

    claimed_first = queue.claim_next("worker", at=NOW)
    assert claimed_first is not None and claimed_first.step_id == "s1"
    assert queue.claim_next("worker", at=NOW) is None
    queue.complete("w1", staff_id="worker", expected_version=2, at=NOW, summary="first done")
    claimed_second = queue.claim_next("worker", at=NOW)
    assert claimed_second is not None and claimed_second.step_id == "s2"


def test_memory_visibility_blocks_private_and_cross_department_leaks(tmp_path) -> None:
    organization, staff = _organization()
    organizations = InMemoryOrganizations(organization)
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    memories = SQLiteMemoryRepository(store)
    memories.save(MemoryEntry("private", "org-1", "chief", MemoryScope.PRIVATE, "Chief secret", NOW))
    memories.save(MemoryEntry("ops", "org-1", "chief", MemoryScope.DEPARTMENT, "Ops context", NOW, "ops"))
    memories.save(MemoryEntry("sales", "org-1", "outsider", MemoryScope.DEPARTMENT, "Sales context", NOW, "sales"))
    memories.save(MemoryEntry("shared", "org-1", "chief", MemoryScope.ORGANIZATION, "Shared context", NOW))

    visible_to_worker = ReadVisibleMemory(memories, staff, organizations)("org-1", "worker")
    assert {entry.memory_id for entry in visible_to_worker} == {"ops", "shared"}


def test_nemotron_parser_rejects_staff_assignment_or_tool_authority() -> None:
    goal = Goal("g1", "org-1", "Goal", "Do governed work", "chief", NOW)
    context = PlanningContext("org-1", goal, "chief", ())
    malicious = """{
      "summary": "bad",
      "steps": [{
        "step_id": "s1", "title": "Write", "action": "write", "resource": "erp",
        "risk": "high", "department_id": "ops", "depends_on": [],
        "assigned_staff_id": "worker"
      }]
    }"""
    with pytest.raises(RuntimeError, match="forbidden|unsupported"):
        NemotronPlanningAdapter.parse_content(malicious, context)


def test_nemotron_parser_returns_non_executing_plan() -> None:
    goal = Goal("g1", "org-1", "Goal", "Do governed work", "chief", NOW)
    context = PlanningContext("org-1", goal, "chief", ())
    safe = """{
      "summary": "safe plan",
      "steps": [{
        "step_id": "s1", "title": "Read CRM", "action": "read", "resource": "crm",
        "risk": "low", "department_id": "sales", "depends_on": []
      }]
    }"""
    proposal = NemotronPlanningAdapter.parse_content(safe, context)
    assert proposal.goal_id == "g1"
    assert proposal.requested_by == "chief"
    assert proposal.steps[0].action == "read"
