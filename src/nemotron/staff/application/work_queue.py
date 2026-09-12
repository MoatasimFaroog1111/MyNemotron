from __future__ import annotations

from nemotron.staff.application.ports import AuditPort, ClockPort, GovernanceAuditEvent, StaffRepository
from nemotron.staff.domain import PermissionDenied, RiskLevel
from nemotron.staff.domain.runtime import WorkItem, WorkStatus

from .runtime_ports import WorkQueueRepository


class Inbox:
    def __init__(self, queue: WorkQueueRepository, staff: StaffRepository) -> None:
        self._queue = queue
        self._staff = staff

    def __call__(self, staff_id: str) -> tuple[WorkItem, ...]:
        self._staff.get(staff_id).assert_active()
        return self._queue.inbox(staff_id)


class ClaimNextWork:
    def __init__(self, queue: WorkQueueRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._queue = queue
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, staff_id: str) -> WorkItem | None:
        member = self._staff.get(staff_id)
        member.assert_active()
        item = self._queue.claim_next(staff_id, at=self._clock.now())
        if item is None:
            return None
        member.assert_allowed(item.action, item.resource, item.risk)
        self._audit.append(GovernanceAuditEvent("work.claimed", "work_item", item.work_item_id, staff_id, self._clock.now(), item.title))
        return item


class CompleteWork:
    def __init__(self, queue: WorkQueueRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._queue = queue
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, work_item_id: str, staff_id: str, *, expected_version: int, summary: str) -> WorkItem:
        member = self._staff.get(staff_id)
        member.assert_active()
        updated = self._queue.complete(
            work_item_id,
            staff_id=staff_id,
            expected_version=expected_version,
            at=self._clock.now(),
            summary=summary,
        )
        if updated.status is not WorkStatus.COMPLETED:
            raise PermissionDenied("Work queue returned a non-completed item after completion.")
        self._audit.append(GovernanceAuditEvent("work.completed", "work_item", work_item_id, staff_id, self._clock.now(), summary))
        return updated
