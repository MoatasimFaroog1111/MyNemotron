from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.application.ports import AuditPort, ClockPort, GovernanceAuditEvent, IdGeneratorPort, StaffRepository
from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.runtime import Goal

from .runtime_ports import GoalRepository


@dataclass(frozen=True, slots=True)
class CreateGoalRequest:
    organization_id: str
    title: str
    description: str
    actor_id: str
    owner_staff_id: str | None = None
    department_id: str | None = None


class CreateGoal:
    def __init__(self, goals: GoalRepository, staff: StaffRepository, ids: IdGeneratorPort, clock: ClockPort, audit: AuditPort) -> None:
        self._goals = goals
        self._staff = staff
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: CreateGoalRequest) -> Goal:
        actor = self._staff.get(request.actor_id)
        actor.assert_allowed("goal.manage", "staff-goals", RiskLevel.MEDIUM)
        if request.owner_staff_id is not None:
            self._staff.get(request.owner_staff_id).assert_active()
        goal = Goal(
            goal_id=self._ids.new_id(),
            organization_id=request.organization_id,
            title=request.title,
            description=request.description,
            created_by=request.actor_id,
            created_at=self._clock.now(),
            owner_staff_id=request.owner_staff_id,
            department_id=request.department_id,
        )
        self._goals.save(goal)
        self._audit.append(GovernanceAuditEvent("goal.created", "goal", goal.goal_id, request.actor_id, goal.created_at, goal.title))
        return goal


class CompleteGoal:
    def __init__(self, goals: GoalRepository, staff: StaffRepository, clock: ClockPort, audit: AuditPort) -> None:
        self._goals = goals
        self._staff = staff
        self._clock = clock
        self._audit = audit

    def __call__(self, goal_id: str, actor_id: str) -> Goal:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("goal.manage", "staff-goals", RiskLevel.MEDIUM)
        updated = self._goals.get(goal_id).complete()
        self._goals.save(updated)
        self._audit.append(GovernanceAuditEvent("goal.completed", "goal", goal_id, actor_id, self._clock.now(), updated.title))
        return updated
