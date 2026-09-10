from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.domain import (
    InvalidTransition,
    PermissionDenied,
    RiskLevel,
    StaffMember,
    StaffStatus,
    Task,
    TaskState,
)
from nemotron.staff.domain.organization import (
    DelegationDenied,
    Department,
    Organization,
    OrganizationError,
    StaffPlacement,
)

from .ports import (
    AuditPort,
    ClockPort,
    GovernanceAuditEvent,
    IdGeneratorPort,
    OrganizationRepository,
    StaffRepository,
    TaskRepository,
)


@dataclass(frozen=True, slots=True)
class RegisterStaffRequest:
    member: StaffMember
    actor_id: str


@dataclass(frozen=True, slots=True)
class CreateOrganizationRequest:
    name: str
    actor_id: str


@dataclass(frozen=True, slots=True)
class DelegationCandidate:
    staff_id: str
    display_name: str
    role_name: str
    department_id: str
    job_title: str


@dataclass(frozen=True, slots=True)
class DelegationProposal:
    task_id: str
    organization_id: str
    chief_of_staff_id: str
    candidates: tuple[DelegationCandidate, ...]
    rationale: str


def _assert_role_can_be_granted(actor: StaffMember, target: StaffMember) -> None:
    for permission in target.role.permissions:
        if not actor.role.allows(permission.action, permission.resource, permission.max_risk):
            raise PermissionDenied(
                f"Staff member {actor.staff_id!r} cannot grant permission "
                f"{permission.action!r} on {permission.resource!r} up to {permission.max_risk.value} risk."
            )

    if target.role.approval_limit is not None and not actor.role.can_approve(target.role.approval_limit):
        raise PermissionDenied(
            f"Staff member {actor.staff_id!r} cannot grant approval authority up to "
            f"{target.role.approval_limit.value} risk."
        )


class RegisterStaff:
    """Register staff without allowing privilege escalation."""

    def __init__(self, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, request: RegisterStaffRequest) -> StaffMember:
        actor = self._staff.get(request.actor_id)
        actor.assert_allowed("staff.register", "organization", RiskLevel.HIGH)
        _assert_role_can_be_granted(actor, request.member)

        if any(member.staff_id == request.member.staff_id for member in self._staff.list_all()):
            raise OrganizationError(f"Staff member {request.member.staff_id!r} is already registered.")

        request.member.assert_active()
        self._staff.save(request.member)
        self._audit.append(
            GovernanceAuditEvent(
                event_type="staff.registered",
                subject_type="staff",
                subject_id=request.member.staff_id,
                actor_id=request.actor_id,
                occurred_at=self._clock.now(),
                detail=f"registered role {request.member.role.name}",
            )
        )
        return request.member


class CreateOrganization:
    def __init__(
        self,
        organizations: OrganizationRepository,
        staff: StaffRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._organizations = organizations
        self._staff = staff
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: CreateOrganizationRequest) -> Organization:
        actor = self._staff.get(request.actor_id)
        actor.assert_allowed("organization.manage", "organization", RiskLevel.HIGH)
        organization = Organization(self._ids.new_id(), request.name)
        self._organizations.save(organization)
        self._audit.append(
            GovernanceAuditEvent(
                "organization.created",
                "organization",
                organization.organization_id,
                request.actor_id,
                self._clock.now(),
                organization.name,
            )
        )
        return organization


class AddDepartment:
    def __init__(
        self,
        organizations: OrganizationRepository,
        staff: StaffRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._organizations = organizations
        self._staff = staff
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        organization_id: str,
        actor_id: str,
        *,
        name: str,
        parent_department_id: str | None = None,
    ) -> Organization:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("organization.manage", "organization", RiskLevel.HIGH)
        organization = self._organizations.get(organization_id)
        department = Department(self._ids.new_id(), name, parent_department_id)
        updated = organization.add_department(department)
        self._organizations.save(updated)
        self._audit.append(
            GovernanceAuditEvent(
                "organization.department_added",
                "department",
                department.department_id,
                actor_id,
                self._clock.now(),
                f"{name} in {organization_id}",
            )
        )
        return updated


class PlaceStaff:
    def __init__(
        self,
        organizations: OrganizationRepository,
        staff: StaffRepository,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._organizations = organizations
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        organization_id: str,
        actor_id: str,
        *,
        staff_id: str,
        department_id: str,
        job_title: str,
        manager_id: str | None = None,
    ) -> Organization:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("organization.manage", "organization", RiskLevel.HIGH)
        member = self._staff.get(staff_id)
        member.assert_active()
        if manager_id is not None:
            self._staff.get(manager_id).assert_active()

        organization = self._organizations.get(organization_id)
        updated = organization.place_staff(
            StaffPlacement(
                staff_id=staff_id,
                department_id=department_id,
                job_title=job_title,
                manager_id=manager_id,
            )
        )
        self._organizations.save(updated)
        self._audit.append(
            GovernanceAuditEvent(
                "organization.staff_placed",
                "staff",
                staff_id,
                actor_id,
                self._clock.now(),
                f"{job_title} in {department_id}",
            )
        )
        return updated


class SetChiefOfStaff:
    def __init__(
        self,
        organizations: OrganizationRepository,
        staff: StaffRepository,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._organizations = organizations
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, organization_id: str, actor_id: str, chief_of_staff_id: str) -> Organization:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("organization.manage", "organization", RiskLevel.CRITICAL)
        chief = self._staff.get(chief_of_staff_id)
        chief.assert_active()

        organization = self._organizations.get(organization_id)
        updated = organization.set_chief_of_staff(chief_of_staff_id)
        self._organizations.save(updated)
        self._audit.append(
            GovernanceAuditEvent(
                "organization.chief_of_staff_set",
                "organization",
                organization_id,
                actor_id,
                self._clock.now(),
                f"chief_of_staff={chief_of_staff_id}",
            )
        )
        return updated


class ChiefOfStaffOrchestrator:
    """Produce a deterministic eligible delegation pool; it never executes tools."""

    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._organizations = organizations

    def propose(
        self,
        task_id: str,
        organization_id: str,
        chief_of_staff_id: str,
        *,
        department_id: str | None = None,
    ) -> DelegationProposal:
        task = self._tasks.get(task_id)
        if task.state is not TaskState.EVIDENCE:
            raise InvalidTransition("Delegation proposals are only allowed during the evidence stage.")

        organization = self._organizations.get(organization_id)
        if organization.chief_of_staff_id != chief_of_staff_id:
            raise DelegationDenied("Only the appointed Chief of Staff may orchestrate organization-wide delegation.")

        chief = self._staff.get(chief_of_staff_id)
        chief.assert_allowed("delegate", task.resource, task.risk)

        members = {member.staff_id: member for member in self._staff.list_all()}
        candidates: list[DelegationCandidate] = []

        for placement in organization.placements:
            if placement.staff_id == chief_of_staff_id:
                continue
            member = members.get(placement.staff_id)
            if member is None or member.status is not StaffStatus.ACTIVE:
                continue
            if department_id is not None and not organization.department_is_within(
                placement.department_id,
                department_id,
            ):
                continue
            if not member.role.allows(task.action, task.resource, task.risk):
                continue

            organization.assert_can_delegate(chief_of_staff_id, member.staff_id)
            candidates.append(
                DelegationCandidate(
                    staff_id=member.staff_id,
                    display_name=member.display_name,
                    role_name=member.role.name,
                    department_id=placement.department_id,
                    job_title=placement.job_title,
                )
            )

        candidates.sort(key=lambda candidate: candidate.staff_id)
        scope = department_id or "organization-wide"
        return DelegationProposal(
            task_id=task.task_id,
            organization_id=organization.organization_id,
            chief_of_staff_id=chief_of_staff_id,
            candidates=tuple(candidates),
            rationale=(
                f"Eligible active staff for {task.action!r} on {task.resource!r} "
                f"at {task.risk.value} risk within {scope} scope."
            ),
        )


class DelegateTask:
    """Assign through the organization hierarchy after explicit delegation authorization."""

    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._organizations = organizations
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        task_id: str,
        organization_id: str,
        delegator_id: str,
        delegatee_id: str,
    ) -> Task:
        task = self._tasks.get(task_id)
        delegator = self._staff.get(delegator_id)
        delegatee = self._staff.get(delegatee_id)
        organization = self._organizations.get(organization_id)

        delegator.assert_allowed("delegate", task.resource, task.risk)
        delegatee.assert_allowed(task.action, task.resource, task.risk)
        organization.assert_can_delegate(delegator_id, delegatee_id)

        updated = task.assign_to(delegatee_id)
        self._tasks.save(updated)
        self._audit.append(
            GovernanceAuditEvent(
                "task.delegated",
                "task",
                task_id,
                delegator_id,
                self._clock.now(),
                f"delegated to {delegatee_id} via {organization_id}",
            )
        )
        return updated
