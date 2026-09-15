from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SkillTrainingError(ValueError):
    """Raised when governed skill-training invariants are violated."""


class TrainingStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    INACTIVE = "inactive"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    name: str
    description: str
    instructions: str
    source_path: str
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.skill_id.strip():
            raise SkillTrainingError("skill_id is required.")
        if not self.name.strip():
            raise SkillTrainingError("skill name is required.")
        if not self.description.strip():
            raise SkillTrainingError("skill description is required.")
        if not self.source_path.strip():
            raise SkillTrainingError("skill source_path is required.")


@dataclass(frozen=True, slots=True)
class SkillVersion:
    version_id: str
    skill: SkillDefinition
    package_sha256: str
    content_sha256: str
    imported_at: datetime
    imported_by: str
    status: TrainingStatus = TrainingStatus.PENDING
    supersedes_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillValidationResult:
    valid: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TrainingRecommendation:
    skill_id: str
    staff_ids: tuple[str, ...]
    confidence: float
    rationale: str


@dataclass(frozen=True, slots=True)
class TrainingAssignment:
    assignment_id: str
    version_id: str
    staff_id: str
    assigned_at: datetime
    assigned_by: str
    status: TrainingStatus = TrainingStatus.ACTIVE
