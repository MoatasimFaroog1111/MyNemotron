from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from nemotron.staff.domain.tools import (
    CapabilityClaims,
    CapabilityToken,
    IdempotencyRecord,
    ToolDefinition,
    ToolExecutionIntent,
    ToolExecutionReceipt,
)


class ToolIntentRepository(Protocol):
    def get(self, task_id: str) -> ToolExecutionIntent:
        """Return the immutable reviewed intent for a task or raise LookupError."""

    def save_if_absent(self, intent: ToolExecutionIntent) -> ToolExecutionIntent:
        """Persist once. Existing identical intent may be returned; conflicting intent must fail."""


class CapabilityAuthorityPort(Protocol):
    def issue(self, claims: CapabilityClaims) -> CapabilityToken:
        """Create a short-lived opaque signed capability token."""

    def verify(self, token: CapabilityToken, *, now: datetime) -> CapabilityClaims:
        """Verify token integrity and expiry and return trusted claims."""


class IdempotencyRepository(Protocol):
    def get(self, key: str) -> IdempotencyRecord | None:
        """Return a prior execution record if one exists."""

    def reserve(self, key: str, fingerprint: str, *, at: datetime) -> IdempotencyRecord:
        """Atomically reserve a key. Conflicting or already-processing records must fail closed."""

    def complete(self, key: str, fingerprint: str, receipt: ToolExecutionReceipt) -> IdempotencyRecord:
        """Atomically store the durable execution receipt."""

    def release_safe_failure(self, key: str, fingerprint: str) -> None:
        """Release only when the adapter proves no external side effect began."""


class ToolAdapterPort(Protocol):
    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        capability_token: CapabilityToken,
        idempotency_key: str,
    ) -> ToolExecutionReceipt:
        """Execute one registered operation after independently validating the capability."""


class ToolRegistryPort(Protocol):
    def definition(self, tool_id: str) -> ToolDefinition:
        """Return trusted tool metadata or raise LookupError."""

    def adapter(self, tool_id: str) -> ToolAdapterPort:
        """Return the registered adapter for the tool."""


class ToolInvocationError(RuntimeError):
    """Adapter failure with explicit side-effect ambiguity metadata."""

    def __init__(self, message: str, *, side_effect_started: bool) -> None:
        super().__init__(message)
        self.side_effect_started = side_effect_started
