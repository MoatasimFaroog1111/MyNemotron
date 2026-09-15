from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nemotron.staff.domain.skills import SkillVersion, TrainingAssignment, TrainingStatus


class SkillRegistryPort(Protocol):
    def activate_assignments(
        self,
        version_id: str,
        staff_ids: tuple[str, ...],
        *,
        assigned_by: str,
        assigned_at: datetime,
    ) -> tuple[TrainingAssignment, ...]: ...

    def resolve_staff(self, staff_id: str) -> tuple[SkillVersion, ...]: ...

    def set_version_status(self, version_id: str, status: TrainingStatus) -> SkillVersion: ...


class AssignSkillTraining:
    def __init__(self, registry: SkillRegistryPort) -> None:
        self.registry = registry

    def __call__(
        self,
        version_id: str,
        staff_ids: tuple[str, ...],
        *,
        assigned_by: str,
        assigned_at: datetime,
    ) -> tuple[TrainingAssignment, ...]:
        targets = tuple(sorted(set(item.strip() for item in staff_ids if item.strip())))
        if not targets:
            raise ValueError("At least one training target is required.")
        return self.registry.activate_assignments(
            version_id,
            targets,
            assigned_by=assigned_by.strip() or "control-plane",
            assigned_at=assigned_at,
        )


class ResolveAssignedSkills:
    def __init__(self, registry: SkillRegistryPort) -> None:
        self.registry = registry

    def __call__(self, staff_id: str) -> tuple[SkillVersion, ...]:
        if not staff_id.strip():
            raise ValueError("staff_id is required.")
        return self.registry.resolve_staff(staff_id.strip())


class DeactivateSkillVersion:
    def __init__(self, registry: SkillRegistryPort) -> None:
        self.registry = registry

    def __call__(self, version_id: str) -> SkillVersion:
        return self.registry.set_version_status(version_id, TrainingStatus.INACTIVE)
