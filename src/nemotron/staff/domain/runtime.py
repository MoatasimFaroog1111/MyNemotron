from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .model import RiskLevel, StaffCoreError


class RuntimeError(StaffCoreError):
    """Raised when the governed staff runtime violates an invariant."""


class MemoryScope(str, Enum):
    PRIVATE = "private"
    DEPARTMENT = "department"
    ORGANIZATION = "organization"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    memory_id: str
    organization_id: str
    owner_staff_id: str
    scope: MemoryScope
    content: str
    created_at: datetime
    department_id: str | None = None
    source_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.memory_id.strip() or not self.organization_id.strip() or not self.owner_staff_id.strip():
            raise RuntimeError("Memory id, organization id, and owner staff id are required.")
        if not self.content.strip():
            raise RuntimeError("Memory content cannot be empty.")
        if self.scope is MemoryScope.DEPARTMENT and not (self.department_id or "").strip():
            raise RuntimeError("Department-scoped memory requires a department id.")
        if self.scope is not MemoryScope.DEPARTMENT and self.department_id is not None:
            raise RuntimeError("Only department-scoped memory may carry a department id.")


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    organization_id: str
    title: str
    description: str
    created_by: str
    created_at: datetime
    owner_staff_id: str | None = None
    department_id: str | None = None
    status: GoalStatus = GoalStatus.ACTIVE

    def __post_init__(self) -> None:
        if not self.goal_id.strip() or not self.organization_id.strip() or not self.title.strip():
            raise RuntimeError("Goal id, organization id, and title are required.")
        if not self.description.strip() or not self.created_by.strip():
            raise RuntimeError("Goal description and creator are required.")

    def complete(self) -> Goal:
        if self.status is not GoalStatus.ACTIVE:
            raise RuntimeError(f"Goal cannot be completed while {self.status.value}.")
        return replace(self, status=GoalStatus.COMPLETED)


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    title: str
    action: str
    resource: str
    risk: RiskLevel
    department_id: str | None = None
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in {
            "step_id": self.step_id,
            "title": self.title,
            "action": self.action,
            "resource": self.resource,
        }.items():
            if not value.strip():
                raise RuntimeError(f"Plan step {name} cannot be empty.")
        if self.step_id in self.depends_on:
            raise RuntimeError("A plan step cannot depend on itself.")


@dataclass(frozen=True, slots=True)
class PlanProposal:
    proposal_id: str
    goal_id: str
    organization_id: str
    requested_by: str
    summary: str
    steps: tuple[PlanStep, ...]
    created_at: datetime
    version: int = 1
    accepted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.proposal_id.strip() or not self.goal_id.strip() or not self.organization_id.strip():
            raise RuntimeError("Proposal id, goal id, and organization id are required.")
        if not self.requested_by.strip() or not self.summary.strip():
            raise RuntimeError("Proposal requester and summary are required.")
        if not self.steps:
            raise RuntimeError("A plan proposal requires at least one step.")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Plan step ids must be unique.")
        known = set(ids)
        for step in self.steps:
            if any(dependency not in known for dependency in step.depends_on):
                raise RuntimeError("Plan step dependency points to an unknown step.")

    def accept(self, at: datetime) -> PlanProposal:
        if self.accepted_at is not None:
            raise RuntimeError("Plan proposal has already been accepted.")
        return replace(self, accepted_at=at, version=self.version + 1)


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: str
    organization_id: str
    goal_id: str
    proposal_id: str
    step_id: str
    title: str
    action: str
    resource: str
    risk: RiskLevel
    assigned_staff_id: str
    created_at: datetime
    depends_on: tuple[str, ...] = ()
    status: WorkStatus = WorkStatus.QUEUED
    version: int = 1
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.work_item_id,
            self.organization_id,
            self.goal_id,
            self.proposal_id,
            self.step_id,
            self.title,
            self.action,
            self.resource,
            self.assigned_staff_id,
        )
        if any(not value.strip() for value in required):
            raise RuntimeError("Work item required fields cannot be empty.")
        if self.version < 1:
            raise RuntimeError("Work item version must be positive.")

    def claim(self, at: datetime) -> WorkItem:
        if self.status is not WorkStatus.QUEUED:
            raise RuntimeError(f"Work item cannot be claimed while {self.status.value}.")
        return replace(self, status=WorkStatus.CLAIMED, claimed_at=at, version=self.version + 1)

    def complete(self, at: datetime, summary: str) -> WorkItem:
        if self.status is not WorkStatus.CLAIMED:
            raise RuntimeError(f"Work item cannot be completed while {self.status.value}.")
        if not summary.strip():
            raise RuntimeError("Work result summary cannot be empty.")
        return replace(
            self,
            status=WorkStatus.COMPLETED,
            completed_at=at,
            result_summary=summary,
            version=self.version + 1,
        )
