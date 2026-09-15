from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from .model import StaffCoreError


class DurableRuntimeError(StaffCoreError):
    """Raised when durable execution invariants are violated."""


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    max_concurrency: int = 4
    lease_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise DurableRuntimeError("max_concurrency must be at least 1.")
        if self.lease_seconds < 1:
            raise DurableRuntimeError("lease_seconds must be at least 1.")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: int = 5
    max_delay_seconds: int = 300
    backoff_factor: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise DurableRuntimeError("max_attempts must be at least 1.")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise DurableRuntimeError("Retry delays cannot be negative.")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise DurableRuntimeError("max_delay_seconds cannot be smaller than initial_delay_seconds.")
        if self.backoff_factor < 1:
            raise DurableRuntimeError("backoff_factor must be at least 1.")

    def delay_for_attempt(self, attempt: int) -> timedelta:
        if attempt < 1:
            raise DurableRuntimeError("Attempt numbers start at 1.")
        seconds = self.initial_delay_seconds * (self.backoff_factor ** (attempt - 1))
        return timedelta(seconds=min(seconds, self.max_delay_seconds))


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    run_id: str
    work_item_id: str
    idempotency_key: str
    status: ExecutionStatus
    attempt: int
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.work_item_id.strip() or not self.idempotency_key.strip():
            raise DurableRuntimeError("Run id, work item id, and idempotency key are required.")
        if self.attempt < 0:
            raise DurableRuntimeError("Run attempt cannot be negative.")
        if self.status is ExecutionStatus.RUNNING:
            if not (self.lease_owner or "").strip() or self.lease_expires_at is None:
                raise DurableRuntimeError("Running executions require an active lease owner and expiry.")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise DurableRuntimeError("Only running executions may hold a lease.")
        if self.status is ExecutionStatus.RETRY_SCHEDULED and self.next_attempt_at is None:
            raise DurableRuntimeError("Scheduled retries require next_attempt_at.")

    @classmethod
    def pending(
        cls,
        *,
        run_id: str,
        work_item_id: str,
        idempotency_key: str,
        at: datetime,
    ) -> ExecutionRun:
        return cls(
            run_id=run_id,
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            status=ExecutionStatus.PENDING,
            attempt=0,
            created_at=at,
            updated_at=at,
        )

    def is_claimable(self, at: datetime) -> bool:
        if self.status is ExecutionStatus.PENDING:
            return True
        if self.status is ExecutionStatus.RETRY_SCHEDULED:
            return self.next_attempt_at is not None and self.next_attempt_at <= at
        if self.status is ExecutionStatus.RUNNING:
            return self.lease_expires_at is not None and self.lease_expires_at <= at
        return False

    def has_active_lease(self, at: datetime) -> bool:
        return (
            self.status is ExecutionStatus.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at > at
        )

    def claim(self, *, worker_id: str, at: datetime, lease_seconds: int) -> ExecutionRun:
        if not worker_id.strip():
            raise DurableRuntimeError("worker_id is required.")
        if lease_seconds < 1:
            raise DurableRuntimeError("lease_seconds must be at least 1.")
        if not self.is_claimable(at):
            raise DurableRuntimeError(f"Execution cannot be claimed while {self.status.value}.")
        return replace(
            self,
            status=ExecutionStatus.RUNNING,
            attempt=self.attempt + 1,
            updated_at=at,
            next_attempt_at=None,
            lease_owner=worker_id,
            lease_expires_at=at + timedelta(seconds=lease_seconds),
        )

    def heartbeat(self, *, worker_id: str, at: datetime, lease_seconds: int) -> ExecutionRun:
        self._assert_live_owner(worker_id, at)
        if lease_seconds < 1:
            raise DurableRuntimeError("lease_seconds must be at least 1.")
        return replace(self, updated_at=at, lease_expires_at=at + timedelta(seconds=lease_seconds))

    def succeed(self, *, worker_id: str, at: datetime) -> ExecutionRun:
        self._assert_live_owner(worker_id, at)
        return replace(
            self,
            status=ExecutionStatus.SUCCEEDED,
            updated_at=at,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error=None,
        )

    def fail(self, *, worker_id: str, at: datetime, error: str, retry_policy: RetryPolicy) -> ExecutionRun:
        self._assert_live_owner(worker_id, at)
        if not error.strip():
            raise DurableRuntimeError("Execution failure must include an error message.")
        if self.attempt >= retry_policy.max_attempts:
            return replace(
                self,
                status=ExecutionStatus.FAILED,
                updated_at=at,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                last_error=error,
            )
        return replace(
            self,
            status=ExecutionStatus.RETRY_SCHEDULED,
            updated_at=at,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=at + retry_policy.delay_for_attempt(self.attempt),
            last_error=error,
        )

    def _assert_live_owner(self, worker_id: str, at: datetime) -> None:
        if self.status is not ExecutionStatus.RUNNING:
            raise DurableRuntimeError("Execution is not running.")
        if self.lease_owner != worker_id:
            raise DurableRuntimeError("Worker does not own this execution lease.")
        if self.lease_expires_at is None or self.lease_expires_at <= at:
            raise DurableRuntimeError("Execution lease has expired.")
