from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nemotron.staff.domain.model import StaffMember, Task


class TaskRepository(Protocol):
    def get(self, task_id: str) -> Task:
        """Return a task or raise LookupError."""

    def save(self, task: Task) -> None:
        """Persist the complete task aggregate."""


class StaffRepository(Protocol):
    def get(self, staff_id: str) -> StaffMember:
        """Return a staff member or raise LookupError."""


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


class ActionExecutorPort(Protocol):
    def execute(self, task: Task) -> ExecutionReceipt:
        """Perform an already-authorized external action and return execution evidence."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: str
    task_id: str
    actor_id: str
    occurred_at: datetime
    detail: str


class AuditPort(Protocol):
    def append(self, event: AuditEvent) -> None:
        """Append an immutable audit event."""
