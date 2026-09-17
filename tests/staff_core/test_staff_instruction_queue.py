from __future__ import annotations

from nemotron.staff.application.queue_staff_instruction import QueueStaffInstructionRequest
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService
from nemotron.staff.domain import Permission, RiskLevel, Role, StaffMember
from nemotron.staff.domain.organization import Department, Organization, StaffPlacement
from nemotron.staff.domain.runtime import MemoryScope


class _NoBackgroundControlPlaneService(ControlPlaneService):
    def _process_staff_instruction(self, staff_id: str, work_item_id: str) -> None:
        return


def _runtime(tmp_path):  # type: ignore[no-untyped-def]
    config = ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="queue-test-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"q" * 32,
        nemotron_base_url="https://model.example.test",
        nemotron_model="nemotron-queue-test",
    )
    runtime = build_production_runtime(config)
    member = StaffMember(
        "staff-queue-1",
        "موظف الطابور",
        Role(
            "role-queue-1",
            "محلل",
            (
                Permission("read", "github", RiskLevel.LOW),
                Permission("memory.read", "staff-memory", RiskLevel.LOW),
            ),
        ),
    )
    runtime.staff.save(member)
    runtime.organizations.save(
        Organization(
            organization_id="org-queue",
            name="منظمة الاختبار",
            departments=(Department("ops", "العمليات"),),
            placements=(StaffPlacement(member.staff_id, "ops", "محلل"),),
        )
    )
    return runtime


def test_shared_queue_use_case_creates_governed_chain_and_one_private_instruction_memory(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    queued = runtime.queue_staff_instruction(
        QueueStaffInstructionRequest(
            staff_id="staff-queue-1",
            instruction="  راجع المستودع وحدد المخاطر.  ",
            actor_id="evaluation-runner",
        )
    )

    goals = runtime.goals.list_active("org-queue")
    assert len(goals) == 1
    assert goals[0].goal_id == queued.goal_id
    assert goals[0].description == "راجع المستودع وحدد المخاطر."

    work_items = runtime.worker_queue.inbox("staff-queue-1")
    assert len(work_items) == 1
    assert work_items[0].work_item_id == queued.work_item_id
    proposal = runtime.plans.get(work_items[0].proposal_id)
    assert proposal.goal_id == queued.goal_id
    assert proposal.accepted_at is not None

    memories = runtime.memories.list_for_organization("org-queue")
    assert len(memories) == 1
    memory = memories[0]
    assert memory.owner_staff_id == "staff-queue-1"
    assert memory.scope is MemoryScope.PRIVATE
    assert memory.content == "راجع المستودع وحدد المخاطر."
    assert memory.source_reference == f"ui-instruction:{queued.work_item_id}"
    assert queued.task_id == f"work-task:{queued.work_item_id}"


def test_control_plane_submit_staff_instruction_keeps_existing_accepted_queue_contract(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    service = _NoBackgroundControlPlaneService(runtime)

    result = service.submit_staff_instruction("staff-queue-1", "راجع حالة النظام.")

    assert result["accepted"] is True
    assert result["staff_id"] == "staff-queue-1"
    assert result["status"] == "queued"
    assert result["execution"]["status"] == "processing"
    assert result["execution"]["task_id"] == f"work-task:{result['work_item_id']}"
    assert result["execution"]["task_state"] is None
