from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias

from nemotron.staff.domain.model import StaffMember, Task
from nemotron.staff.domain.organization import Organization


class TaskRepository(Protocol):
    def get(self, task_id: str) -> Task:
        """Return a task or raise LookupError."""

    def save(self, task: Task) -> None:
        """Persist the complete task aggregate."""


class StaffRepository(Protocol):
    def get(self, staff_id: str) -> StaffMember:
        """Return a staff member or raise LookupError."""

    def save(self, member: StaffMember) -> None:
        """Create or replace one staff registry member."""

    def list_all(self) -> tuple[StaffMember, ...]:
        """Return the complete staff registry snapshot."""


class OrganizationRepository(Protocol):
    def get(self, organization_id: str) -> Organization:
        """Return an organization or raise LookupError."""

    def save(self, organization: Organization) -> None:
        """Persist the complete organization aggregate."""


class ClockPort(Protocol):
    def now(self) -> datetime:
        """Return the current application time."""


class IdGeneratorPort(Protocol):
    def new_id(self) -> str:
        """Return a new unique id."""


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    reference: str
    summary: str

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("Execution receipt reference cannot be empty.")
        if not self.summary.strip():
            raise ValueError("Execution receipt summary cannot be empty.")


class ActionExecutorPort(Protocol):
    def execute(self, task: Task, *, idempotency_key: str) -> ExecutionReceipt:
        """Perform an authorized external action idempotently and return execution evidence."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: str
    task_id: str
    actor_id: str
    occurred_at: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class GovernanceAuditEvent:
    event_type: str
    subject_type: str
    subject_id: str
    actor_id: str
    occurred_at: datetime
    detail: str


AuditRecord: TypeAlias = AuditEvent | GovernanceAuditEvent


class AuditPort(Protocol):
    def append(self, event: AuditRecord) -> None:
        """Append an immutable audit record."""
