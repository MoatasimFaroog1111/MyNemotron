from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.application.ports import AuditPort, ClockPort, GovernanceAuditEvent, IdGeneratorPort, OrganizationRepository, StaffRepository
from nemotron.staff.domain import PermissionDenied, RiskLevel
from nemotron.staff.domain.runtime import PlanProposal, WorkItem

from .memory import ReadVisibleMemory
from .runtime_ports import GoalRepository, PlanRepository, PlanningContext, PlanningPort, WorkQueueRepository


class BuildPlanProposal:
    """Ask a planner for a proposal only. No queue writes or external side effects happen here."""

    def __init__(
        self,
        goals: GoalRepository,
        plans: PlanRepository,
        planner: PlanningPort,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        memories: ReadVisibleMemory,
        audit: AuditPort,
        clock: ClockPort,
    ) -> None:
        self._goals = goals
        self._plans = plans
        self._planner = planner
        self._staff = staff
        self._organizations = organizations
        self._memories = memories
        self._audit = audit
        self._clock = clock

    def __call__(self, goal_id: str, requester_id: str) -> PlanProposal:
        requester = self._staff.get(requester_id)
        requester.assert_allowed("plan.propose", "staff-planning", RiskLevel.MEDIUM)
        goal = self._goals.get(goal_id)
        self._organizations.get(goal.organization_id).placement_for(requester_id)
        context = PlanningContext(
            organization_id=goal.organization_id,
            goal=goal,
            requested_by=requester_id,
            visible_memory=self._memories(goal.organization_id, requester_id),
        )
        proposal = self._planner.propose(context)
        if proposal.goal_id != goal.goal_id or proposal.organization_id != goal.organization_id:
            raise PermissionDenied("Planner returned a proposal outside the requested goal or organization.")
        if proposal.requested_by != requester_id:
            raise PermissionDenied("Planner changed the trusted requester identity.")
        self._plans.save(proposal)
        self._audit.append(GovernanceAuditEvent("plan.proposed", "plan", proposal.proposal_id, requester_id, self._clock.now(), proposal.summary))
        return proposal


@dataclass(frozen=True, slots=True)
class AcceptPlanRequest:
    proposal_id: str
    actor_id: str
    expected_version: int
    assignments: dict[str, str]


class AcceptPlan:
    """Trusted boundary that validates LLM steps, staff assignments, and permissions before queueing work."""

    def __init__(
        self,
        plans: PlanRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._plans = plans
        self._staff = staff
        self._organizations = organizations
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: AcceptPlanRequest) -> tuple[WorkItem, ...]:
        actor = self._staff.get(request.actor_id)
        actor.assert_allowed("plan.accept", "staff-planning", RiskLevel.HIGH)
        proposal = self._plans.get(request.proposal_id)
        if proposal.version != request.expected_version:
            raise PermissionDenied("Plan proposal version changed; review is required before acceptance.")
        organization = self._organizations.get(proposal.organization_id)
        if organization.chief_of_staff_id != request.actor_id:
            raise PermissionDenied("Only the appointed Chief of Staff may accept a staff plan.")
        step_ids = {step.step_id for step in proposal.steps}
        if set(request.assignments) != step_ids:
            raise PermissionDenied("Every plan step must receive exactly one trusted staff assignment.")

        now = self._clock.now()
        items: list[WorkItem] = []
        for step in proposal.steps:
            assignee_id = request.assignments[step.step_id]
            assignee = self._staff.get(assignee_id)
            assignee.assert_allowed(step.action, step.resource, step.risk)
            organization.assert_can_delegate(request.actor_id, assignee_id)
            if step.department_id is not None:
                placement = organization.placement_for(assignee_id)
                if not organization.department_is_within(placement.department_id, step.department_id):
                    raise PermissionDenied(f"Assignee {assignee_id!r} is outside step department scope.")
            items.append(
                WorkItem(
                    work_item_id=self._ids.new_id(),
                    organization_id=proposal.organization_id,
                    goal_id=proposal.goal_id,
                    proposal_id=proposal.proposal_id,
                    step_id=step.step_id,
                    title=step.title,
                    action=step.action,
                    resource=step.resource,
                    risk=step.risk,
                    assigned_staff_id=assignee_id,
                    created_at=now,
                    depends_on=step.depends_on,
                )
            )

        accepted = proposal.accept(now)
        work_items = tuple(items)
        self._plans.accept_with_work_items(accepted, expected_version=request.expected_version, work_items=work_items)
        self._audit.append(GovernanceAuditEvent("plan.accepted", "plan", proposal.proposal_id, request.actor_id, now, f"queued {len(work_items)} work items"))
        return work_items
