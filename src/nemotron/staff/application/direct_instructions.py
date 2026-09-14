from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.application.ports import AuditPort, ClockPort, GovernanceAuditEvent, IdGeneratorPort, OrganizationRepository, StaffRepository
from nemotron.staff.application.runtime_ports import GoalRepository, PlanRepository, WorkQueueRepository
from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.runtime import Goal, PlanProposal, PlanStep, WorkItem


@dataclass(frozen=True, slots=True)
class SubmitDirectInstructionRequest:
    staff_id: str
    instruction: str
    actor_id: str = "ui-operator"


class SubmitDirectInstruction:
    """Queue one low-risk instruction for a selected staff member.

    This adapter intentionally creates a governed Goal -> accepted Plan -> WorkItem
    chain instead of bypassing Staff Core. The worker still performs reasoning and
    all external side effects remain subject to the existing tool/approval gates.
    """

    def __init__(
        self,
        *,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        goals: GoalRepository,
        plans: PlanRepository,
        queue: WorkQueueRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._staff = staff
        self._organizations = organizations
        self._goals = goals
        self._plans = plans
        self._queue = queue
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: SubmitDirectInstructionRequest) -> WorkItem:
        instruction = request.instruction.strip()
        if not instruction:
            raise ValueError("instruction cannot be empty")
        if len(instruction) > 8_000:
            raise ValueError("instruction is too long")

        member = self._staff.get(request.staff_id)
        member.assert_active()

        organization = None
        placement = None
        for candidate in self._organizations.list_all():
            try:
                placement = candidate.placement_for(request.staff_id)
            except LookupError:
                continue
            organization = candidate
            break
        if organization is None or placement is None:
            raise LookupError(request.staff_id)

        # The browser instruction is deliberately analysis-only. It grants no
        # external tool authority. A later governed flow must prepare/approve
        # any concrete side effect separately.
        member.assert_allowed("read", "*", RiskLevel.LOW)
        now = self._clock.now()
        goal_id = self._ids.new_id()
        proposal_id = self._ids.new_id()
        step_id = self._ids.new_id()
        work_item_id = self._ids.new_id()
        title = instruction.splitlines()[0][:160]

        goal = Goal(
            goal_id=goal_id,
            organization_id=organization.organization_id,
            title=title,
            description=instruction,
            created_by=request.actor_id,
            created_at=now,
            owner_staff_id=request.staff_id,
            department_id=placement.department_id,
        )
        self._goals.save(goal)

        step = PlanStep(
            step_id=step_id,
            title=title,
            action="read",
            resource="*",
            risk=RiskLevel.LOW,
            department_id=placement.department_id,
        )
        proposal = PlanProposal(
            proposal_id=proposal_id,
            goal_id=goal_id,
            organization_id=organization.organization_id,
            requested_by=request.actor_id,
            summary="Direct UI instruction queued for governed staff reasoning.",
            steps=(step,),
            created_at=now,
            accepted_at=now,
        )
        self._plans.save(proposal)

        item = WorkItem(
            work_item_id=work_item_id,
            organization_id=organization.organization_id,
            goal_id=goal_id,
            proposal_id=proposal_id,
            step_id=step_id,
            title=title,
            action="read",
            resource="*",
            risk=RiskLevel.LOW,
            assigned_staff_id=request.staff_id,
            created_at=now,
        )
        self._queue.enqueue(item)
        self._audit.append(
            GovernanceAuditEvent(
                "ui.instruction_submitted",
                "work_item",
                item.work_item_id,
                request.actor_id,
                now,
                f"assigned_staff_id={request.staff_id}",
            )
        )
        return item
