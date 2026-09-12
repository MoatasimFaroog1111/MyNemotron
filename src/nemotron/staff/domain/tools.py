from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .model import RiskLevel, StaffCoreError


class ToolGatewayError(StaffCoreError):
    """Raised when an execution/tool-gateway invariant is violated."""


class ToolCategory(str, Enum):
    GITHUB = "github"
    EMAIL = "email"
    ERP = "erp"
    BROWSER = "browser"
    FILES = "files"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class ToolOperation:
    name: str
    task_actions: tuple[str, ...]
    minimum_risk: RiskLevel
    mutating: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ToolGatewayError("Tool operation name cannot be empty.")
        if not self.task_actions or any(not action.strip() for action in self.task_actions):
            raise ToolGatewayError("Tool operation requires at least one task action.")

    def allows_task(self, action: str, risk: RiskLevel) -> bool:
        return ("*" in self.task_actions or action in self.task_actions) and risk.severity >= self.minimum_risk.severity


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    tool_id: str
    category: ToolCategory
    resource: str
    operations: tuple[ToolOperation, ...]

    def __post_init__(self) -> None:
        if not self.tool_id.strip() or not self.resource.strip():
            raise ToolGatewayError("Tool id and resource cannot be empty.")
        if not self.operations:
            raise ToolGatewayError("A tool requires at least one operation.")
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ToolGatewayError("Tool operation names must be unique.")

    def operation(self, name: str) -> ToolOperation:
        for operation in self.operations:
            if operation.name == name:
                return operation
        raise ToolGatewayError(f"Tool {self.tool_id!r} does not expose operation {name!r}.")

    def matches_resource(self, resource: str) -> bool:
        return self.resource == "*" or self.resource == resource


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    if not isinstance(arguments, Mapping):
        raise ToolGatewayError("Tool arguments must be a mapping.")
    try:
        return json.dumps(
            dict(arguments),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ToolGatewayError("Tool arguments must be JSON serializable.") from exc


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolExecutionIntent:
    task_id: str
    actor_id: str
    tool_id: str
    operation: str
    canonical_arguments_json: str
    arguments_sha256: str
    prepared_at: datetime

    def __post_init__(self) -> None:
        required = (self.task_id, self.actor_id, self.tool_id, self.operation, self.arguments_sha256)
        if any(not value.strip() for value in required):
            raise ToolGatewayError("Tool execution intent required fields cannot be empty.")
        if len(self.arguments_sha256) != 64:
            raise ToolGatewayError("Tool execution intent requires a SHA-256 digest.")
        try:
            parsed = json.loads(self.canonical_arguments_json)
        except json.JSONDecodeError as exc:
            raise ToolGatewayError("Tool execution intent arguments are not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ToolGatewayError("Tool execution intent arguments must be a JSON object.")
        actual = hashlib.sha256(self.canonical_arguments_json.encode("utf-8")).hexdigest()
        if actual != self.arguments_sha256:
            raise ToolGatewayError("Tool execution intent arguments digest does not match payload.")

    @classmethod
    def prepare(
        cls,
        *,
        task_id: str,
        actor_id: str,
        tool_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        prepared_at: datetime,
    ) -> ToolExecutionIntent:
        canonical = canonical_arguments(arguments)
        return cls(
            task_id=task_id,
            actor_id=actor_id,
            tool_id=tool_id,
            operation=operation,
            canonical_arguments_json=canonical,
            arguments_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            prepared_at=prepared_at,
        )

    def arguments(self) -> dict[str, Any]:
        value = json.loads(self.canonical_arguments_json)
        if not isinstance(value, dict):
            raise ToolGatewayError("Stored tool arguments are not a JSON object.")
        return value

    def assert_matches(self, arguments: Mapping[str, Any]) -> None:
        if arguments_digest(arguments) != self.arguments_sha256:
            raise ToolGatewayError("Execution arguments differ from the reviewed tool intent.")

    @property
    def fingerprint(self) -> str:
        raw = f"{self.task_id}|{self.actor_id}|{self.tool_id}|{self.operation}|{self.arguments_sha256}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    task_id: str
    actor_id: str
    tool_id: str
    operation: str
    arguments_sha256: str
    idempotency_key: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        values = (
            self.task_id,
            self.actor_id,
            self.tool_id,
            self.operation,
            self.arguments_sha256,
            self.idempotency_key,
        )
        if any(not value.strip() for value in values):
            raise ToolGatewayError("Capability claims required fields cannot be empty.")
        if self.expires_at <= self.issued_at:
            raise ToolGatewayError("Capability expiration must be after issuance.")


@dataclass(frozen=True, slots=True)
class CapabilityToken:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ToolGatewayError("Capability token cannot be empty.")


@dataclass(frozen=True, slots=True)
class ToolExecutionReceipt:
    tool_id: str
    operation: str
    reference: str
    summary: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.tool_id, self.operation, self.reference, self.summary)):
            raise ToolGatewayError("Tool execution receipt fields cannot be empty.")


class IdempotencyState(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: str
    fingerprint: str
    state: IdempotencyState
    created_at: datetime
    receipt: ToolExecutionReceipt | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.fingerprint.strip():
            raise ToolGatewayError("Idempotency key and fingerprint cannot be empty.")
        if self.state is IdempotencyState.COMPLETED and self.receipt is None:
            raise ToolGatewayError("Completed idempotency record requires a receipt.")
