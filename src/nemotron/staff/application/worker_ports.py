from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from nemotron.staff.domain.model import Task
from nemotron.staff.domain.runtime import Goal, MemoryEntry, WorkItem
from nemotron.staff.domain.skills import SkillVersion


class WorkerAnalysisStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkerContext:
    organization_id: str
    staff_id: str
    role_name: str
    goal: Goal
    work_item: WorkItem
    task: Task
    visible_memory: tuple[MemoryEntry, ...]
    approved_skills: tuple[SkillVersion, ...] = ()

    def __post_init__(self) -> None:
        if not self.organization_id.strip() or not self.staff_id.strip() or not self.role_name.strip():
            raise ValueError("Worker context organization, staff, and role are required.")
        if self.goal.organization_id != self.organization_id:
            raise ValueError("Worker goal is outside the context organization.")
        if self.work_item.organization_id != self.organization_id:
            raise ValueError("Worker item is outside the context organization.")
        if self.work_item.assigned_staff_id != self.staff_id:
            raise ValueError("Worker item is assigned to another staff member.")
        if self.task.assignee_id != self.staff_id:
            raise ValueError("Governed task is assigned to another staff member.")
        if any(version.status.value != "active" for version in self.approved_skills):
            raise ValueError("Worker context may contain only active approved skill versions.")
        version_ids = [version.version_id for version in self.approved_skills]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("Worker context approved skill versions must be unique.")


@dataclass(frozen=True, slots=True)
class WorkerAnalysis:
    status: WorkerAnalysisStatus
    work_summary: str
    evidence_memory_ids: tuple[str, ...] = ()
    decision_rationale: str | None = None
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.work_summary.strip():
            raise ValueError("Worker analysis requires a work summary.")
        if len(self.evidence_memory_ids) != len(set(self.evidence_memory_ids)):
            raise ValueError("Worker analysis evidence ids must be unique.")
        if any(not value.strip() for value in self.evidence_memory_ids):
            raise ValueError("Worker analysis evidence ids cannot be empty.")
        if self.status is WorkerAnalysisStatus.READY:
            if not self.evidence_memory_ids:
                raise ValueError("A ready worker analysis requires evidence.")
            if not (self.decision_rationale or "").strip():
                raise ValueError("A ready worker analysis requires decision rationale.")
            if self.block_reason is not None:
                raise ValueError("A ready worker analysis cannot include a block reason.")
        elif self.status is WorkerAnalysisStatus.BLOCKED:
            if not (self.block_reason or "").strip():
                raise ValueError("A blocked worker analysis requires a block reason.")
            if self.decision_rationale is not None:
                raise ValueError("A blocked worker analysis cannot include decision rationale.")


class WorkerReasoningPort(Protocol):
    def analyze(self, context: WorkerContext) -> WorkerAnalysis:
        """Reason over trusted context only. Implementations must never execute tools or mutate state."""


class WorkerQueuePort(Protocol):
    def claim_next(self, staff_id: str, *, at: datetime) -> WorkItem | None:
        """Atomically claim the next eligible work item, recovering expired leases."""

    def claim_work_item(self, work_item_id: str, *, staff_id: str, at: datetime) -> WorkItem | None:
        """Atomically claim one specific eligible work item without draining unrelated backlog."""

    def heartbeat(self, work_item_id: str, *, staff_id: str, at: datetime) -> None:
        """Extend a live worker lease without changing the work-item version."""

    def complete(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        summary: str,
    ) -> WorkItem:
        """Complete a claimed work item."""

    def release(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        reason: str,
    ) -> None:
        """Return claimed work immediately to the queue for non-retry cleanup."""

    def schedule_retry(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        reason: str,
    ) -> datetime:
        """Schedule a transient failure for a future retry and return that time."""

    def block(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        reason: str,
    ) -> None:
        """Move claimed work to blocked for human review."""

    def attempts(self, work_item_id: str) -> int:
        """Return how many times the worker runtime has claimed this item."""
