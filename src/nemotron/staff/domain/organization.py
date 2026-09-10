from __future__ import annotations

from dataclasses import dataclass, replace

from .model import PermissionDenied, StaffCoreError


class OrganizationError(StaffCoreError):
    """Raised when the organization structure violates an invariant."""


class DelegationDenied(PermissionDenied):
    """Raised when a staff member cannot delegate to another staff member."""


@dataclass(frozen=True, slots=True)
class Department:
    department_id: str
    name: str
    parent_department_id: str | None = None

    def __post_init__(self) -> None:
        if not self.department_id.strip():
            raise OrganizationError("Department id cannot be empty.")
        if not self.name.strip():
            raise OrganizationError("Department name cannot be empty.")
        if self.parent_department_id == self.department_id:
            raise OrganizationError("A department cannot be its own parent.")


@dataclass(frozen=True, slots=True)
class StaffPlacement:
    staff_id: str
    department_id: str
    job_title: str
    manager_id: str | None = None

    def __post_init__(self) -> None:
        if not self.staff_id.strip():
            raise OrganizationError("Placement staff id cannot be empty.")
        if not self.department_id.strip():
            raise OrganizationError("Placement department id cannot be empty.")
        if not self.job_title.strip():
            raise OrganizationError("Placement job title cannot be empty.")
        if self.manager_id == self.staff_id:
            raise OrganizationError("A staff member cannot manage themselves.")


@dataclass(frozen=True, slots=True)
class Organization:
    organization_id: str
    name: str
    departments: tuple[Department, ...] = ()
    placements: tuple[StaffPlacement, ...] = ()
    chief_of_staff_id: str | None = None

    def __post_init__(self) -> None:
        if not self.organization_id.strip():
            raise OrganizationError("Organization id cannot be empty.")
        if not self.name.strip():
            raise OrganizationError("Organization name cannot be empty.")
        self._validate_structure()

    def _validate_structure(self) -> None:
        department_by_id = {department.department_id: department for department in self.departments}
        if len(department_by_id) != len(self.departments):
            raise OrganizationError("Department ids must be unique.")

        placement_by_staff = {placement.staff_id: placement for placement in self.placements}
        if len(placement_by_staff) != len(self.placements):
            raise OrganizationError("Each staff member may have only one active placement.")

        for department in self.departments:
            if (
                department.parent_department_id is not None
                and department.parent_department_id not in department_by_id
            ):
                raise OrganizationError(
                    f"Parent department {department.parent_department_id!r} is not part of the organization."
                )

        for placement in self.placements:
            if placement.department_id not in department_by_id:
                raise OrganizationError(
                    f"Department {placement.department_id!r} is not part of the organization."
                )
            if placement.manager_id is not None and placement.manager_id not in placement_by_staff:
                raise OrganizationError(f"Manager {placement.manager_id!r} has no organization placement.")

        self._assert_no_department_cycles(department_by_id)
        self._assert_no_management_cycles(placement_by_staff)

        if self.chief_of_staff_id is not None and self.chief_of_staff_id not in placement_by_staff:
            raise OrganizationError("Chief of Staff must have an organization placement.")

    def _assert_no_department_cycles(self, department_by_id: dict[str, Department]) -> None:
        for department in self.departments:
            seen = {department.department_id}
            parent_id = department.parent_department_id
            while parent_id is not None:
                if parent_id in seen:
                    raise OrganizationError("Department hierarchy contains a cycle.")
                seen.add(parent_id)
                parent_id = department_by_id[parent_id].parent_department_id

    def _assert_no_management_cycles(self, placement_by_staff: dict[str, StaffPlacement]) -> None:
        for placement in self.placements:
            seen = {placement.staff_id}
            manager_id = placement.manager_id
            while manager_id is not None:
                if manager_id in seen:
                    raise OrganizationError("Management hierarchy contains a cycle.")
                seen.add(manager_id)
                manager_id = placement_by_staff[manager_id].manager_id

    def add_department(self, department: Department) -> Organization:
        if any(existing.department_id == department.department_id for existing in self.departments):
            raise OrganizationError(f"Department {department.department_id!r} already exists.")
        return replace(self, departments=(*self.departments, department))

    def place_staff(self, placement: StaffPlacement) -> Organization:
        if any(existing.staff_id == placement.staff_id for existing in self.placements):
            raise OrganizationError(f"Staff member {placement.staff_id!r} already has a placement.")
        return replace(self, placements=(*self.placements, placement))

    def set_chief_of_staff(self, staff_id: str) -> Organization:
        if not staff_id.strip():
            raise OrganizationError("Chief of Staff id cannot be empty.")
        self.placement_for(staff_id)
        return replace(self, chief_of_staff_id=staff_id)

    def placement_for(self, staff_id: str) -> StaffPlacement:
        for placement in self.placements:
            if placement.staff_id == staff_id:
                return placement
        raise OrganizationError(f"Staff member {staff_id!r} has no organization placement.")

    def department_for(self, staff_id: str) -> Department:
        placement = self.placement_for(staff_id)
        for department in self.departments:
            if department.department_id == placement.department_id:
                return department
        raise OrganizationError(f"Department {placement.department_id!r} is unavailable.")

    def management_chain(self, staff_id: str) -> tuple[str, ...]:
        placement_by_staff = {placement.staff_id: placement for placement in self.placements}
        placement = self.placement_for(staff_id)
        chain: list[str] = []
        manager_id = placement.manager_id
        while manager_id is not None:
            chain.append(manager_id)
            manager_id = placement_by_staff[manager_id].manager_id
        return tuple(chain)

    def manages(self, manager_id: str, staff_id: str) -> bool:
        return manager_id in self.management_chain(staff_id)

    def department_is_within(self, department_id: str, ancestor_department_id: str) -> bool:
        department_by_id = {department.department_id: department for department in self.departments}
        if department_id not in department_by_id:
            raise OrganizationError(f"Department {department_id!r} is not part of the organization.")
        if ancestor_department_id not in department_by_id:
            raise OrganizationError(
                f"Department {ancestor_department_id!r} is not part of the organization."
            )

        current_id: str | None = department_id
        while current_id is not None:
            if current_id == ancestor_department_id:
                return True
            current_id = department_by_id[current_id].parent_department_id
        return False

    def assert_can_delegate(self, delegator_id: str, delegatee_id: str) -> None:
        self.placement_for(delegator_id)
        self.placement_for(delegatee_id)
        if delegator_id == delegatee_id:
            raise DelegationDenied("A staff member cannot delegate a task to themselves.")
        if delegator_id == self.chief_of_staff_id:
            return
        if self.manages(delegator_id, delegatee_id):
            return
        raise DelegationDenied(
            f"Staff member {delegator_id!r} is outside the management chain for {delegatee_id!r}."
        )
