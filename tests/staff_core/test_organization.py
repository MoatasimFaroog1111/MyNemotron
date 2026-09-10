from datetime import datetime, timezone

import pytest

from nemotron.staff.application.organization import (
    ChiefOfStaffOrchestrator,
    DelegateTask,
    RegisterStaff,
    RegisterStaffRequest,
)
from nemotron.staff.application.ports import AuditRecord
from nemotron.staff.domain import (
    Permission,
    PermissionDenied,
    RiskLevel,
    Role,
    StaffMember,
    StaffStatus,
    Task,
)
from nemotron.staff.domain.organization import (
    DelegationDenied,
    Department,
    Organization,
    OrganizationError,
    StaffPlacement,
)


NOW = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)


class InMemoryTasks:
    def __init__(self, *tasks: Task) -> None:
        self.items = {task.task_id: task for task in tasks}

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

    def save(self, member: StaffMember) -> None:
        self.items[member.staff_id] = member

    def list_all(self) -> tuple[StaffMember, ...]:
        return tuple(self.items.values())


class InMemoryOrganizations:
    def __init__(self, *organizations: Organization) -> None:
        self.items = {organization.organization_id: organization for organization in organizations}

    def get(self, organization_id: str) -> Organization:
        try:
            return self.items[organization_id]
        except KeyError as exc:
            raise LookupError(organization_id) from exc

    def save(self, organization: Organization) -> None:
        self.items[organization.organization_id] = organization


class FixedClock:
    def now(self) -> datetime:
        return NOW


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditRecord] = []

    def append(self, event: AuditRecord) -> None:
        self.events.append(event)


def role(role_id: str, name: str, *permissions: Permission, approval_limit: RiskLevel | None = None) -> Role:
    return Role(role_id, name, permissions, approval_limit=approval_limit)


def build_company() -> tuple[InMemoryStaff, InMemoryOrganizations, InMemoryTasks, InMemoryAudit]:
    chief = StaffMember(
        "chief-1",
        "Chief of Staff",
        role("chief", "Chief of Staff", Permission("delegate", "erp", RiskLevel.CRITICAL)),
    )
    ops_manager = StaffMember(
        "ops-manager",
        "Operations Manager",
        role("ops-manager-role", "Operations Manager", Permission("delegate", "erp", RiskLevel.HIGH)),
    )
    ops_worker = StaffMember(
        "ops-worker",
        "Operations Specialist",
        role("ops-worker-role", "Operations Specialist", Permission("write", "erp", RiskLevel.HIGH)),
    )
    suspended_worker = StaffMember(
        "suspended-worker",
        "Suspended Specialist",
        role("suspended-role", "Suspended Specialist", Permission("write", "erp", RiskLevel.HIGH)),
        status=StaffStatus.SUSPENDED,
    )
    finance_worker = StaffMember(
        "finance-worker",
        "Finance Specialist",
        role("finance-worker-role", "Finance Specialist", Permission("write", "erp", RiskLevel.HIGH)),
    )

    departments = (
        Department("company", "Company"),
        Department("operations", "Operations", "company"),
        Department("finance", "Finance", "company"),
    )
    placements = (
        StaffPlacement("chief-1", "company", "Chief of Staff"),
        StaffPlacement("ops-manager", "operations", "Operations Manager", "chief-1"),
        StaffPlacement("ops-worker", "operations", "Operations Specialist", "ops-manager"),
        StaffPlacement("suspended-worker", "operations", "Operations Specialist", "ops-manager"),
        StaffPlacement("finance-worker", "finance", "Finance Specialist", "chief-1"),
    )
    organization = Organization(
        "org-1",
        "MyNemotron AI Company",
        departments=departments,
        placements=placements,
        chief_of_staff_id="chief-1",
    )
    task = Task(
        task_id="task-1",
        title="Update ERP record",
        action="write",
        resource="erp",
        risk=RiskLevel.HIGH,
        created_by="owner",
        created_at=NOW,
    )
    return (
        InMemoryStaff(chief, ops_manager, ops_worker, suspended_worker, finance_worker),
        InMemoryOrganizations(organization),
        InMemoryTasks(task),
        InMemoryAudit(),
    )


def test_register_staff_blocks_privilege_escalation() -> None:
    registrar = StaffMember(
        "registrar",
        "Registrar",
        role(
            "registrar-role",
            "Registrar",
            Permission("staff.register", "organization", RiskLevel.HIGH),
        ),
    )
    target = StaffMember(
        "power-user",
        "Power User",
        role("power-role", "Power User", Permission("write", "erp", RiskLevel.HIGH)),
    )
    staff = InMemoryStaff(registrar)
    audit = InMemoryAudit()

    with pytest.raises(PermissionDenied, match="cannot grant permission"):
        RegisterStaff(staff, FixedClock(), audit)(
            RegisterStaffRequest(member=target, actor_id="registrar")
        )

    assert "power-user" not in staff.items
    assert audit.events == []


def test_organization_rejects_management_cycle() -> None:
    with pytest.raises(OrganizationError, match="cycle"):
        Organization(
            "org-cycle",
            "Broken Company",
            departments=(Department("company", "Company"),),
            placements=(
                StaffPlacement("a", "company", "A", "b"),
                StaffPlacement("b", "company", "B", "a"),
            ),
        )


def test_chief_of_staff_proposes_only_active_eligible_staff_in_scope() -> None:
    staff, organizations, tasks, _ = build_company()

    proposal = ChiefOfStaffOrchestrator(tasks, staff, organizations).propose(
        "task-1",
        "org-1",
        "chief-1",
        department_id="operations",
    )

    assert [candidate.staff_id for candidate in proposal.candidates] == ["ops-worker"]
    assert proposal.organization_id == "org-1"
    assert proposal.chief_of_staff_id == "chief-1"


def test_non_chief_cannot_run_organization_wide_orchestration() -> None:
    staff, organizations, tasks, _ = build_company()

    with pytest.raises(DelegationDenied, match="Chief of Staff"):
        ChiefOfStaffOrchestrator(tasks, staff, organizations).propose(
            "task-1",
            "org-1",
            "ops-manager",
        )


def test_management_chain_blocks_cross_department_delegation() -> None:
    staff, organizations, tasks, audit = build_company()
    delegate = DelegateTask(tasks, staff, organizations, FixedClock(), audit)

    with pytest.raises(DelegationDenied, match="outside the management chain"):
        delegate("task-1", "org-1", "ops-manager", "finance-worker")

    assert tasks.get("task-1").assignee_id is None
    assert audit.events == []


def test_chief_of_staff_can_delegate_across_departments_with_explicit_permission() -> None:
    staff, organizations, tasks, audit = build_company()

    updated = DelegateTask(tasks, staff, organizations, FixedClock(), audit)(
        "task-1",
        "org-1",
        "chief-1",
        "finance-worker",
    )

    assert updated.assignee_id == "finance-worker"
    assert tasks.get("task-1").assignee_id == "finance-worker"
    assert len(audit.events) == 1
    assert audit.events[0].event_type == "task.delegated"
