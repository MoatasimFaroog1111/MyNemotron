from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .model import StaffCoreError


class KnowledgeError(StaffCoreError):
    """Raised when institutional-memory invariants are violated."""


class KnowledgeScope(str, Enum):
    PRIVATE = "private"
    DEPARTMENT = "department"
    ORGANIZATION = "organization"


class KnowledgeSourceKind(str, Enum):
    DOCUMENT = "document"
    LOG = "log"
    MANUAL_EVIDENCE = "manual_evidence"


class CorrectionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    knowledge_id: str
    organization_id: str
    owner_staff_id: str
    scope: KnowledgeScope
    content: str
    source_kind: KnowledgeSourceKind
    source_reference: str
    source_observed_at: datetime
    recorded_at: datetime
    department_id: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.knowledge_id,
            self.organization_id,
            self.owner_staff_id,
            self.content,
            self.source_reference,
        )
        if any(not value.strip() for value in required):
            raise KnowledgeError("Knowledge id, organization, owner, content, and source reference are required.")
        if self.scope is KnowledgeScope.DEPARTMENT and not (self.department_id or "").strip():
            raise KnowledgeError("Department-scoped knowledge requires department_id.")
        if self.scope is not KnowledgeScope.DEPARTMENT and self.department_id is not None:
            raise KnowledgeError("Only department-scoped knowledge may carry department_id.")
        if self.source_observed_at > self.recorded_at:
            raise KnowledgeError("Knowledge source date cannot be later than the recording date.")

    @property
    def is_active(self) -> bool:
        return self.superseded_by is None

    def supersede(self, replacement_id: str) -> KnowledgeRecord:
        if not replacement_id.strip():
            raise KnowledgeError("Replacement id is required.")
        if not self.is_active:
            raise KnowledgeError("Knowledge record has already been superseded.")
        if replacement_id == self.knowledge_id:
            raise KnowledgeError("Knowledge record cannot supersede itself.")
        return replace(self, superseded_by=replacement_id)


@dataclass(frozen=True, slots=True)
class KnowledgeCorrection:
    correction_id: str
    organization_id: str
    target_knowledge_id: str
    proposed_content: str
    source_kind: KnowledgeSourceKind
    source_reference: str
    source_observed_at: datetime
    proposed_by: str
    proposed_at: datetime
    status: CorrectionStatus = CorrectionStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.correction_id,
            self.organization_id,
            self.target_knowledge_id,
            self.proposed_content,
            self.source_reference,
            self.proposed_by,
        )
        if any(not value.strip() for value in required):
            raise KnowledgeError("Correction ids, content, source, and proposer are required.")
        if self.source_observed_at > self.proposed_at:
            raise KnowledgeError("Correction source date cannot be later than proposal time.")
        if self.status is CorrectionStatus.PENDING:
            if self.decided_by is not None or self.decided_at is not None:
                raise KnowledgeError("Pending correction cannot have a decision identity/time.")
        elif not (self.decided_by or "").strip() or self.decided_at is None:
            raise KnowledgeError("Decided correction requires decision identity/time.")

    def decide(self, *, approver_id: str, approved: bool, at: datetime, rationale: str) -> KnowledgeCorrection:
        if self.status is not CorrectionStatus.PENDING:
            raise KnowledgeError("Correction has already been decided.")
        if not approver_id.strip() or not rationale.strip():
            raise KnowledgeError("Correction decision requires approver and rationale.")
        if approver_id == self.proposed_by:
            raise KnowledgeError("Correction approval must be independent from its proposer.")
        return replace(
            self,
            status=CorrectionStatus.APPROVED if approved else CorrectionStatus.REJECTED,
            decided_by=approver_id,
            decided_at=at,
            rationale=rationale,
        )
