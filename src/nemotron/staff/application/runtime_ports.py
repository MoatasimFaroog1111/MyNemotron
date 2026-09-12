from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nemotron.staff.domain.runtime import Goal, MemoryEntry, PlanProposal, WorkItem


class MemoryRepository(Protocol):
    def save(self, entry: MemoryEntry) -> None:
        """Persist one immutable memory entry."""

    def list_for_organization(self, organization_id: str) -> tuple[MemoryEntry, ...]:
        """Return organization memory entries for application-side access filtering."""


class GoalRepository(Protocol):
    def get(self, goal_id: str) -> Goal:
        """Return a goal or raise LookupError."""

    def save(self, goal: Goal) -> None:
        """Create or replace one goal."""

    def list_active(self, organization_id: str) -> tuple[Goal, ...]:
        """Return active goals for the organization."""


class PlanRepository(Protocol):
    def get(self, proposal_id: str) -> PlanProposal:
        """Return a plan proposal or raise LookupError."""

    def save(self, proposal: PlanProposal) -> None:
        """Create or replace one plan proposal."""

    def accept_with_work_items(
        self,
        proposal: PlanProposal,
        *,
        expected_version: int,
        work_items: tuple[WorkItem, ...],
    ) -> None:
        """Atomically accept a proposal and enqueue all generated work items."""


class WorkQueueRepository(Protocol):
    def enqueue(self, item: WorkItem) -> None:
        """Persist one queued work item."""

    def inbox(self, staff_id: str) -> tuple[WorkItem, ...]:
        """Return current queued/claimed work for one staff member."""

    def claim_next(self, staff_id: str, *, at: datetime) -> WorkItem | None:
        """Atomically claim the next eligible item for one staff member."""

    def complete(
        self,
        work_item_id: str,
        *,
        staff_id: str,
        expected_version: int,
        at: datetime,
        summary: str,
    ) -> WorkItem:
        """Complete a claimed item using optimistic concurrency."""


@dataclass(frozen=True, slots=True)
class PlanningContext:
    organization_id: str
    goal: Goal
    requested_by: str
    visible_memory: tuple[MemoryEntry, ...]


class PlanningPort(Protocol):
    def propose(self, context: PlanningContext) -> PlanProposal:
        """Return a non-executing plan proposal. Implementations must not perform side effects."""
