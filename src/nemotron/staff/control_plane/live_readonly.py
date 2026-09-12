from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from nemotron.staff.application.goals import CreateGoalRequest
from nemotron.staff.application.memory import MemoryScope
from nemotron.staff.application.planning import AcceptPlanRequest
from nemotron.staff.application.tool_gateway import PrepareToolExecutionRequest
from nemotron.staff.domain import Permission, RiskLevel, Role, StaffMember, TaskState
from nemotron.staff.domain.organization import Department, Organization, StaffPlacement

from .config import ControlPlaneConfig
from .runtime import ProductionRuntime, build_production_runtime


@dataclass(frozen=True, slots=True)
class ReadOnlyResult:
    goal_id: str
    proposal_id: str
    task_id: str
    final_state: str
    tool: str
    operation: str
    execution_reference: str
    repository: str
    path: str
    bytes_read: int
    audit_events: int


def _seed(runtime: ProductionRuntime, *, prefix: str) -> dict[str, str]:
    chief_id = f"{prefix}-chief"
    worker_id = f"{prefix}-worker"
    verifier_id = f"{prefix}-verifier"
    organization_id = f"{prefix}-org"
    department_id = f"{prefix}-research"

    chief = StaffMember(
        chief_id,
        "Read-only Chief of Staff",
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
        "Read-only GitHub Researcher",
        Role(
            f"{prefix}-worker-role",
            "GitHub Researcher",
            (
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
                Permission("read", "github", RiskLevel.LOW),
            ),
        ),
    )
    verifier = StaffMember(
        verifier_id,
        "Read-only Verifier",
        Role(
            f"{prefix}-verifier-role",
            "Verifier",
            (Permission("verify", "github", RiskLevel.LOW),),
        ),
    )
    for member in (chief, worker, verifier):
        runtime.staff.save(member)

    runtime.organizations.save(
        Organization(
            organization_id=organization_id,
            name="Read-only Production Check",
            departments=(Department(department_id, "Research"),),
            placements=(
                StaffPlacement(chief_id, department_id, "Chief of Staff"),
                StaffPlacement(worker_id, department_id, "GitHub Researcher", chief_id),
                StaffPlacement(verifier_id, department_id, "Verifier", chief_id),
            ),
            chief_of_staff_id=chief_id,
        )
    )
    return {
        "chief": chief_id,
        "worker": worker_id,
        "verifier": verifier_id,
        "organization": organization_id,
        "department": department_id,
    }


def run_read_only_github(
    runtime: ProductionRuntime,
    *,
    repository: str,
    ref: str = "main",
    path: str = "README.md",
    prefix: str = "readonly",
) -> ReadOnlyResult:
    owner, separator, repo = repository.partition("/")
    if not separator or not owner or not repo or "/" in repo:
        raise ValueError("Repository must be in owner/name format.")
    ids = _seed(runtime, prefix=prefix)
    goal = runtime.create_goal(
        CreateGoalRequest(
            organization_id=ids["organization"],
            title="Read a public GitHub file",
            description=(
                f"Create exactly one plan step that reads {path} from {repository}@{ref}. "
                "The step must use action 'read', resource 'github', risk 'low', "
                "and the Research department. It must not write, comment, create, edit, or delete anything."
            ),
            actor_id=ids["chief"],
            owner_staff_id=ids["worker"],
            department_id=ids["department"],
        )
    )
    runtime.write_memory(
        ids["organization"],
        ids["chief"],
        scope=MemoryScope.ORGANIZATION,
        content=(
            f"Read only {path} from GitHub repository {repository} at ref {ref}. "
            "No external mutation is authorized."
        ),
        source_reference=f"readonly://github/{repository}/{ref}/{path}",
    )
    proposal = runtime.build_plan(goal.goal_id, ids["chief"])
    if len(proposal.steps) != 1:
        raise AssertionError("Read-only planner must return exactly one step.")
    step = proposal.steps[0]
    if (step.action, step.resource, step.risk) != ("read", "github", RiskLevel.LOW):
        raise AssertionError("Planner returned authority outside read/github/low.")

    runtime.accept_plan(
        AcceptPlanRequest(
            proposal_id=proposal.proposal_id,
            actor_id=ids["chief"],
            expected_version=proposal.version,
            assignments={step.step_id: ids["worker"]},
        )
    )
    worker_result = runtime.worker.run_once(ids["worker"])
    if worker_result.task_id is None or worker_result.task_state is not TaskState.READY_FOR_EXECUTION:
        raise AssertionError("Read-only worker did not reach READY_FOR_EXECUTION.")
    task_id = worker_result.task_id

    runtime.prepare_tool_execution(
        PrepareToolExecutionRequest(
            task_id=task_id,
            actor_id=ids["worker"],
            tool_id="github",
            operation="get_file",
            arguments={"owner": owner, "repo": repo, "ref": ref, "path": path},
        )
    )
    execution = runtime.execute_tool_task(task_id, ids["worker"])
    if execution.task.state is not TaskState.VERIFYING:
        raise AssertionError("Read-only GitHub execution did not enter verification.")
    output = execution.receipt.output()
    if output.get("full_name") != repository or output.get("path") != path:
        raise AssertionError("GitHub read returned an unexpected repository or path.")
    bytes_read = int(output.get("bytes") or 0)
    if bytes_read <= 0 or not str(output.get("content") or "").strip():
        raise AssertionError("GitHub read returned empty content.")

    final = runtime.verify_task(
        task_id,
        ids["verifier"],
        passed=True,
        summary="Verified read-only GitHub file receipt; no mutation operation was registered or authorized.",
    )
    if final.state is not TaskState.COMPLETED:
        raise AssertionError("Read-only task did not complete.")

    timeline = runtime.queries.audit_timeline(limit=200)
    return ReadOnlyResult(
        goal_id=goal.goal_id,
        proposal_id=proposal.proposal_id,
        task_id=task_id,
        final_state=final.state.value,
        tool=execution.receipt.tool_id,
        operation=execution.receipt.operation,
        execution_reference=execution.receipt.reference,
        repository=repository,
        path=path,
        bytes_read=bytes_read,
        audit_events=len(timeline),
    )


def main() -> int:
    repository = os.environ.get("STAFF_GITHUB_ALLOWED_REPOSITORIES", "").split(",", 1)[0].strip()
    base_url = os.environ.get("NEMOTRON_BASE_URL", "").strip()
    model = os.environ.get("NEMOTRON_MODEL", "").strip()
    if not repository or not base_url or not model:
        raise SystemExit(
            "STAFF_GITHUB_ALLOWED_REPOSITORIES, NEMOTRON_BASE_URL and NEMOTRON_MODEL are required."
        )
    with tempfile.TemporaryDirectory(prefix="mynemotron-readonly-") as directory:
        root = Path(directory)
        config = ControlPlaneConfig(
            data_dir=root / "data",
            files_root=root / "files",
            host="127.0.0.1",
            port=8088,
            api_token=secrets.token_urlsafe(32),
            capability_secret=secrets.token_bytes(32),
            nemotron_base_url=base_url,
            nemotron_model=model,
            nemotron_api_key=os.environ.get("NEMOTRON_API_KEY") or None,
            github_allowed_repositories=(repository,),
            github_read_only=True,
        )
        runtime = build_production_runtime(config)
        result = run_read_only_github(runtime, repository=repository, prefix="live-ro")
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
