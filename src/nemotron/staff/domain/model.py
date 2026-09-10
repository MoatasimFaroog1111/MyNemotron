from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class StaffCoreError(ValueError):
    """Base domain error for Staff Core invariant violations."""


class InvalidTransition(StaffCoreError):
    """Raised when a task attempts an invalid lifecycle transition."""


class PermissionDenied(StaffCoreError):
    """Raised when a staff member is not allowed to perform an action."""


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return {
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }[self]


class StaffStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TaskState(str, Enum):
    EVIDENCE = "evidence"
    AWAITING_APPROVAL = "awaiting_approval"
    READY_FOR_EXECUTION = "ready_for_execution"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Permission:
    action: str
    resource: str = "*"
    max_risk: RiskLevel = RiskLevel.LOW

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise StaffCoreError("Permission action cannot be empty.")
        if not self.resource.strip():
            raise StaffCoreError("Permission resource cannot be empty.")

    def allows(self, action: str, resource: str, risk: RiskLevel) -> bool:
        action_matches = self.action == "*" or self.action == action
        resource_matches = self.resource == "*" or self.resource == resource
        return action_matches and resource_matches and risk.severity <= self.max_risk.severity


@dataclass(frozen=True, slots=True)
class Role:
    role_id: str
    name: str
    permissions: tuple[Permission, ...]
    approval_limit: RiskLevel | None = None

    def __post_init__(self) -> None:
        if not self.role_id.strip():
            raise StaffCoreError("Role id cannot be empty.")
        if not self.name.strip():
            raise StaffCoreError("Role name cannot be empty.")

    def allows(self, action: str, resource: str, risk: RiskLevel) -> bool:
        return any(permission.allows(action, resource, risk) for permission in self.permissions)

    def can_approve(self, risk: RiskLevel) -> bool:
        return self.approval_limit is not None and risk.severity <= self.approval_limit.severity


@dataclass(frozen=True, slots=True)
class StaffMember:
    staff_id: str
    display_name: str
    role: Role
    status: StaffStatus = StaffStatus.ACTIVE

    def __post_init__(self) -> None:
        if not self.staff_id.strip():
            raise StaffCoreError("Staff id cannot be empty.")
        if not self.display_name.strip():
            raise StaffCoreError("Staff display name cannot be empty.")

    def assert_active(self) -> None:
        if self.status is not StaffStatus.ACTIVE:
            raise PermissionDenied(f"Staff member {self.staff_id} is not active.")

    def assert_allowed(self, action: str, resource: str, risk: RiskLevel) -> None:
        self.assert_active()
        if not self.role.allows(action, resource, risk):
            raise PermissionDenied(
                f"Staff member {self.staff_id} cannot perform {action!r} on {resource!r} at {risk.value} risk."
            )


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    reference: str
    summary: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.reference.strip() or not self.summary.strip():
            raise StaffCoreError("Evidence source, reference, and summary are required.")


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    rationale: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if not self.action.strip() or not self.rationale.strip():
            raise StaffCoreError("Decision action and rationale are required.")


@dataclass(frozen=True, slots=True)
class Approval:
    approver_id: str
    outcome: ApprovalOutcome
    rationale: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if not self.approver_id.strip() or not self.rationale.strip():
            raise StaffCoreError("Approver id and rationale are required.")


@dataclass(frozen=True, slots=True)
class Verification:
    verifier_id: str
    passed: bool
    summary: str
    verified_at: datetime

    def __post_init__(self) -> None:
        if not self.verifier_id.strip() or not self.summary.strip():
            raise StaffCoreError("Verifier id and verification summary are required.")


@dataclass(frozen=True, slots=True)
class GovernancePolicy:
    approval_required_for: frozenset[RiskLevel]
    independent_verification_required_for: frozenset[RiskLevel]

    def requires_approval(self, risk: RiskLevel) -> bool:
        return risk in self.approval_required_for

    def requires_independent_verification(self, risk: RiskLevel) -> bool:
        return risk in self.independent_verification_required_for

    @classmethod
    def conservative(cls) -> GovernancePolicy:
        return cls(
            approval_required_for=frozenset({RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}),
            independent_verification_required_for=frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL}),
        )


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    title: str
    action: str
    resource: str
    risk: RiskLevel
    created_by: str
    created_at: datetime
    state: TaskState = TaskState.EVIDENCE
    assignee_id: str | None = None
    evidence: tuple[Evidence, ...] = ()
    decision: Decision | None = None
    approval: Approval | None = None
    execution_reference: str | None = None
    verification: Verification | None = None

    def __post_init__(self) -> None:
        for field_name, value in {
            "task_id": self.task_id,
            "title": self.title,
            "action": self.action,
            "resource": self.resource,
            "created_by": self.created_by,
        }.items():
            if not value.strip():
                raise StaffCoreError(f"{field_name} cannot be empty.")

    @property
    def execution_key(self) -> str:
        """Stable idempotency key that external action adapters must honor."""
        return f"staff-task:{self.task_id}:execute"

    def assign_to(self, staff_id: str) -> Task:
        if self.state is not TaskState.EVIDENCE:
            raise InvalidTransition("A task cannot be reassigned after its decision lifecycle has started.")
        if not staff_id.strip():
            raise StaffCoreError("Assignee id cannot be empty.")
        return replace(self, assignee_id=staff_id)

    def add_evidence(self, item: Evidence) -> Task:
        if self.state is not TaskState.EVIDENCE:
            raise InvalidTransition("Evidence can only be added before the decision is recorded.")
        return replace(self, evidence=(*self.evidence, item))

    def record_decision(self, decision: Decision, *, approval_required: bool) -> Task:
        if self.state is not TaskState.EVIDENCE:
            raise InvalidTransition(f"Cannot record a decision while task is {self.state.value}.")
        if self.assignee_id is None:
            raise InvalidTransition("Task must be assigned before a decision is recorded.")
        if not self.evidence:
            raise InvalidTransition("At least one evidence item is required before a decision.")
        next_state = TaskState.AWAITING_APPROVAL if approval_required else TaskState.READY_FOR_EXECUTION
        return replace(self, decision=decision, state=next_state)

    def record_approval(self, approval: Approval) -> Task:
        if self.state is not TaskState.AWAITING_APPROVAL:
            raise InvalidTransition(f"Cannot approve task while it is {self.state.value}.")
        next_state = TaskState.READY_FOR_EXECUTION if approval.outcome is ApprovalOutcome.APPROVED else TaskState.BLOCKED
        return replace(self, approval=approval, state=next_state)

    def assert_ready_for_execution(self) -> None:
        if self.state is not TaskState.READY_FOR_EXECUTION:
            raise InvalidTransition(f"Task is not execution-ready; current state is {self.state.value}.")
        if self.assignee_id is None or self.decision is None:
            raise InvalidTransition("Execution-ready task is missing assignee or decision.")

    def record_execution(self, reference: str) -> Task:
        self.assert_ready_for_execution()
        if not reference.strip():
            raise StaffCoreError("Execution reference cannot be empty.")
        return replace(self, execution_reference=reference, state=TaskState.VERIFYING)

    def record_verification(self, verification: Verification) -> Task:
        if self.state is not TaskState.VERIFYING or self.execution_reference is None:
            raise InvalidTransition("Task must have an execution result before verification.")
        next_state = TaskState.COMPLETED if verification.passed else TaskState.BLOCKED
        return replace(self, verification=verification, state=next_state)
