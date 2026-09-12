from __future__ import annotations

from nemotron.staff.application.ports import AuditPort, ClockPort, GovernanceAuditEvent, IdGeneratorPort, OrganizationRepository, StaffRepository
from nemotron.staff.domain import PermissionDenied, RiskLevel
from nemotron.staff.domain.runtime import MemoryEntry, MemoryScope

from .runtime_ports import MemoryRepository


class WriteMemory:
    def __init__(self, memories: MemoryRepository, staff: StaffRepository, organizations: OrganizationRepository, ids: IdGeneratorPort, clock: ClockPort, audit: AuditPort) -> None:
        self._memories = memories
        self._staff = staff
        self._organizations = organizations
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, organization_id: str, actor_id: str, *, scope: MemoryScope, content: str, source_reference: str | None = None) -> MemoryEntry:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("memory.write", "staff-memory", RiskLevel.MEDIUM)
        organization = self._organizations.get(organization_id)
        placement = organization.placement_for(actor_id)
        department_id = placement.department_id if scope is MemoryScope.DEPARTMENT else None
        entry = MemoryEntry(self._ids.new_id(), organization_id, actor_id, scope, content, self._clock.now(), department_id, source_reference)
        self._memories.save(entry)
        self._audit.append(GovernanceAuditEvent("memory.written", "memory", entry.memory_id, actor_id, entry.created_at, scope.value))
        return entry


class ReadVisibleMemory:
    def __init__(self, memories: MemoryRepository, staff: StaffRepository, organizations: OrganizationRepository) -> None:
        self._memories = memories
        self._staff = staff
        self._organizations = organizations

    def __call__(self, organization_id: str, requester_id: str) -> tuple[MemoryEntry, ...]:
        requester = self._staff.get(requester_id)
        requester.assert_allowed("memory.read", "staff-memory", RiskLevel.LOW)
        organization = self._organizations.get(organization_id)
        requester_department = organization.placement_for(requester_id).department_id
        visible: list[MemoryEntry] = []
        for entry in self._memories.list_for_organization(organization_id):
            if entry.scope is MemoryScope.ORGANIZATION:
                visible.append(entry)
            elif entry.scope is MemoryScope.PRIVATE and entry.owner_staff_id == requester_id:
                visible.append(entry)
            elif entry.scope is MemoryScope.DEPARTMENT and entry.department_id == requester_department:
                visible.append(entry)
        return tuple(visible)
