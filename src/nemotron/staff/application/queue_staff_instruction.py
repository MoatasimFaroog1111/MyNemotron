from __future__ import annotations

from dataclasses import dataclass

from nemotron.staff.application.direct_instructions import (
    SubmitDirectInstruction,
    SubmitDirectInstructionRequest,
)
from nemotron.staff.application.ports import (
    AuditPort,
    ClockPort,
    GovernanceAuditEvent,
    IdGeneratorPort,
)
from nemotron.staff.application.runtime_ports import MemoryRepository
from nemotron.staff.domain.runtime import MemoryEntry, MemoryScope


@dataclass(frozen=True, slots=True)
class QueueStaffInstructionRequest:
    staff_id: str
    instruction: str
    actor_id: str = "ui-operator"


@dataclass(frozen=True, slots=True)
class QueuedStaffInstruction:
    work_item_id: str
    goal_id: str
    task_id: str
    staff_id: str
    title: str


class QueueStaffInstruction:
    """Queue one governed staff instruction and persist its exact private memory evidence."""

    def __init__(
        self,
        *,
        submit_direct_instruction: SubmitDirectInstruction,
        memories: MemoryRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._submit_direct_instruction = submit_direct_instruction
        self._memories = memories
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(self, request: QueueStaffInstructionRequest) -> QueuedStaffInstruction:
        instruction = request.instruction.strip()
        item = self._submit_direct_instruction(
            SubmitDirectInstructionRequest(
                staff_id=request.staff_id,
                instruction=instruction,
                actor_id=request.actor_id,
            )
        )

        created_at = self._clock.now()
        memory = MemoryEntry(
            memory_id=self._ids.new_id(),
            organization_id=item.organization_id,
            owner_staff_id=request.staff_id,
            scope=MemoryScope.PRIVATE,
            content=instruction,
            created_at=created_at,
            source_reference=f"ui-instruction:{item.work_item_id}",
        )
        self._memories.save(memory)
        self._audit.append(
            GovernanceAuditEvent(
                "ui.instruction_memory_recorded",
                "memory",
                memory.memory_id,
                request.actor_id,
                created_at,
                f"work_item_id={item.work_item_id}; staff_id={request.staff_id}",
            )
        )

        return QueuedStaffInstruction(
            work_item_id=item.work_item_id,
            goal_id=item.goal_id,
            task_id=f"work-task:{item.work_item_id}",
            staff_id=item.assigned_staff_id,
            title=item.title,
        )
