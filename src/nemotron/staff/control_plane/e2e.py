from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.application.goals import CreateGoalRequest
from nemotron.staff.application.memory import MemoryScope
from nemotron.staff.application.planning import AcceptPlanRequest
from nemotron.staff.application.tool_gateway import PrepareToolExecutionRequest
from nemotron.staff.domain import Permission, RiskLevel, Role, StaffMember, TaskState
from nemotron.staff.domain.organization import Department, Organization, StaffPlacement

from .runtime import ProductionRuntime


@dataclass(frozen=True, slots=True)
class E2EResult:
    goal_id: str
    proposal_id: str
    work_item_ids: tuple[str, ...]
    task_id: str
    final_state: str
    execution_reference: str
    output_file: str
    audit_events: int


def seed_e2e_staff(runtime: ProductionRuntime, *, prefix: str = "e2e") -> dict[str, str]:
    chief_id = f"{prefix}-chief"
    worker_id = f"{prefix}-worker"
    approver_id = f"{prefix}-approver"
    verifier_id = f"{prefix}-verifier"
    organization_id = f"{prefix}-org"
    department_id = f"{prefix}-ops"

    chief = StaffMember(
        chief_id,
        "E2E Chief of Staff",
        Role(
            f"{prefix}-chief-role",
            "Chief of Staff",
            (
                Permission("goal.manage", "staff-goals", RiskLevel.MEDIUM),
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
                Permission("memory.write", "staff-memory", RiskLevel.MEDIUM),
                Permission("plan.propose", "staff-planning", RiskLevel.MEDIUM),
                Permission("plan.accept", "staff-planning", RiskLevel.HIGH),
            ),
        ),
    )
    worker = StaffMember(
        worker_id,
        "E2E Worker",
        Role(
            f"{prefix}-worker-role",
            "File Worker",
            (
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
                Permission("write", "files", RiskLevel.MEDIUM),
            ),
        ),
    )
    approver = StaffMember(
        approver_id,
        "E2E Approver",
        Role(
            f"{prefix}-approver-role",
            "Approver",
            (Permission("approve", "files", RiskLevel.MEDIUM),),
            approval_limit=RiskLevel.MEDIUM,
        ),
    )
    verifier = StaffMember(
        verifier_id,
        "E2E Verifier",
        Role(
            f"{prefix}-verifier-role",
            "Verifier",
            (Permission("verify", "files", RiskLevel.MEDIUM),),
        ),
    )
    for member in (chief, worker, approver, verifier):
        runtime.staff.save(member)

    runtime.organizations.save(
        Organization(
            organization_id=organization_id,
            name="E2E Governed Staff",
            departments=(Department(department_id, "Operations"),),
            placements=(
                StaffPlacement(chief_id, department_id, "Chief of Staff"),
                StaffPlacement(worker_id, department_id, "File Worker", chief_id),
                StaffPlacement(approver_id, department_id, "Approver", chief_id),
                StaffPlacement(verifier_id, department_id, "Verifier", chief_id),
            ),
            chief_of_staff_id=chief_id,
        )
    )
    return {
        "chief": chief_id,
        "worker": worker_id,
        "approver": approver_id,
        "verifier": verifier_id,
        "organization": organization_id,
        "department": department_id,
    }


def run_governed_e2e(runtime: ProductionRuntime, *, prefix: str = "e2e") -> E2EResult:
    ids = seed_e2e_staff(runtime, prefix=prefix)
    output_relative = f"{prefix}/governed-output.txt"
    expected_content = "MyNemotron governed E2E execution succeeded."

    goal = runtime.create_goal(
        CreateGoalRequest(
            organization_id=ids["organization"],
            title="Governed file execution E2E",
            description=(
                "Create exactly one plan step that writes a text file. The step must use action 'write', "
                "resource 'files', risk 'medium', and the Operations department."
            ),
            actor_id=ids["chief"],
            owner_staff_id=ids["worker"],
            department_id=ids["department"],
        )
    )
    memory = runtime.write_memory(
        ids["organization"],
        ids["chief"],
        scope=MemoryScope.ORGANIZATION,
        content=f"Write {output_relative} with content: {expected_content}",
        source_reference=f"e2e://{prefix}/approved-request",
    )
    proposal = runtime.build_plan(goal.goal_id, ids["chief"])
    if len(proposal.steps) != 1:
        raise AssertionError("E2E planner must return exactly one governed step.")
    step = proposal.steps[0]
    if (step.action, step.resource, step.risk) != ("write", "files", RiskLevel.MEDIUM):
        raise AssertionError("E2E planner returned authority outside the expected write/files/medium contract.")

    work_items = runtime.accept_plan(
        AcceptPlanRequest(
            proposal_id=proposal.proposal_id,
            actor_id=ids["chief"],
            expected_version=proposal.version,
            assignments={step.step_id: ids["worker"]},
        )
    )
    worker_result = runtime.worker.run_once(ids["worker"])
    if worker_result.task_id is None or worker_result.task_state is not TaskState.AWAITING_APPROVAL:
        raise AssertionError("Worker did not hand the medium-risk task to approval.")
    task_id = worker_result.task_id

    approval_items = runtime.queries.approval_inbox()
    if not any(item.task_id == task_id for item in approval_items):
        raise AssertionError("Approval inbox did not surface the governed task.")

    runtime.prepare_tool_execution(
        PrepareToolExecutionRequest(
            task_id=task_id,
            actor_id=ids["worker"],
            tool_id="files",
            operation="write_text",
            arguments={"path": output_relative, "content": expected_content},
        )
    )
    runtime.approve_task(
        task_id,
        ids["approver"],
        approved=True,
        rationale="Reviewed exact file path, content digest, risk, and assignee.",
    )
    execution = runtime.execute_tool_task(task_id, ids["worker"])
    if execution.task.state is not TaskState.VERIFYING:
        raise AssertionError("Tool gateway did not hand execution to verification.")

    output_path = runtime.config.files_root / output_relative
    if output_path.read_text(encoding="utf-8") != expected_content:
        raise AssertionError("Real Files adapter side effect did not match the approved content.")

    final = runtime.verify_task(
        task_id,
        ids["verifier"],
        passed=True,
        summary="Verified the governed file exists and exactly matches approved content.",
    )
    if final.state is not TaskState.COMPLETED:
        raise AssertionError("E2E task did not reach COMPLETED after independent verification.")

    timeline = runtime.queries.audit_timeline(limit=200)
    event_types = {item["event_type"] for item in timeline}
    required_events = {
        "goal.created",
        "memory.written",
        "plan.proposed",
        "plan.accepted",
        "worker.decision_handoff",
        "tool.intent_prepared",
        "task.approved",
        "task.executed_via_gateway",
        "task.verified",
    }
    missing = required_events - event_types
    if missing:
        raise AssertionError(f"E2E audit trail is missing events: {sorted(missing)}")

    if memory.memory_id not in {entry.memory_id for entry in runtime.read_memory(ids["organization"], ids["worker"])}:
        raise AssertionError("Worker cannot read the organization memory used as governed evidence.")

    return E2EResult(
        goal_id=goal.goal_id,
        proposal_id=proposal.proposal_id,
        work_item_ids=tuple(item.work_item_id for item in work_items),
        task_id=task_id,
        final_state=final.state.value,
        execution_reference=execution.receipt.reference,
        output_file=str(output_path),
        audit_events=len(timeline),
    )
