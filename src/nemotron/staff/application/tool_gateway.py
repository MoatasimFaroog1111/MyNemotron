from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from nemotron.staff.application.ports import AuditEvent, AuditPort, ClockPort, StaffRepository, TaskRepository
from nemotron.staff.domain.model import ApprovalOutcome, PermissionDenied, Task, TaskState
from nemotron.staff.domain.tools import (
    CapabilityClaims,
    IdempotencyState,
    ToolDefinition,
    ToolExecutionIntent,
    ToolExecutionReceipt,
    ToolGatewayError,
)

from .tool_ports import (
    CapabilityAuthorityPort,
    IdempotencyRepository,
    ToolAdapterPort,
    ToolIntentRepository,
    ToolInvocationError,
    ToolRegistryPort,
)


class InMemoryToolRegistry(ToolRegistryPort):
    """Composition-time registry. Agents never receive registry or adapter objects."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._adapters: dict[str, ToolAdapterPort] = {}

    def register(self, definition: ToolDefinition, adapter: ToolAdapterPort) -> None:
        if definition.tool_id in self._definitions:
            raise ToolGatewayError(f"Tool {definition.tool_id!r} is already registered.")
        self._definitions[definition.tool_id] = definition
        self._adapters[definition.tool_id] = adapter

    def definition(self, tool_id: str) -> ToolDefinition:
        try:
            return self._definitions[tool_id]
        except KeyError as exc:
            raise LookupError(tool_id) from exc

    def adapter(self, tool_id: str) -> ToolAdapterPort:
        try:
            return self._adapters[tool_id]
        except KeyError as exc:
            raise LookupError(tool_id) from exc


@dataclass(frozen=True, slots=True)
class PrepareToolExecutionRequest:
    task_id: str
    actor_id: str
    tool_id: str
    operation: str
    arguments: Mapping[str, Any]


class PrepareToolExecution:
    """Freeze a concrete tool invocation before approval/execution."""

    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        intents: ToolIntentRepository,
        registry: ToolRegistryPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._tasks = tasks
        self._staff = staff
        self._intents = intents
        self._registry = registry
        self._clock = clock
        self._audit = audit

    @staticmethod
    def _validate_binding(task: Task, definition: ToolDefinition, operation_name: str) -> None:
        if not definition.matches_resource(task.resource):
            raise PermissionDenied(
                f"Tool {definition.tool_id!r} is not registered for task resource {task.resource!r}."
            )
        operation = definition.operation(operation_name)
        if not operation.allows_task(task.action, task.risk):
            raise PermissionDenied(
                f"Operation {operation.name!r} cannot serve action {task.action!r} at {task.risk.value} risk."
            )

    def __call__(self, request: PrepareToolExecutionRequest) -> ToolExecutionIntent:
        task = self._tasks.get(request.task_id)
        actor = self._staff.get(request.actor_id)
        if task.assignee_id != request.actor_id:
            raise PermissionDenied("Only the assigned staff member may prepare the tool execution intent.")
        actor.assert_allowed(task.action, task.resource, task.risk)
        if task.state not in {TaskState.EVIDENCE, TaskState.AWAITING_APPROVAL, TaskState.READY_FOR_EXECUTION}:
            raise ToolGatewayError(f"Tool intent cannot be prepared while task is {task.state.value}.")

        definition = self._registry.definition(request.tool_id)
        self._validate_binding(task, definition, request.operation)
        intent = ToolExecutionIntent.prepare(
            task_id=task.task_id,
            actor_id=request.actor_id,
            tool_id=request.tool_id,
            operation=request.operation,
            arguments=request.arguments,
            prepared_at=self._clock.now(),
        )
        stored = self._intents.save_if_absent(intent)
        self._audit.append(
            AuditEvent(
                "tool.intent_prepared",
                task.task_id,
                request.actor_id,
                self._clock.now(),
                f"{stored.tool_id}:{stored.operation}:{stored.arguments_sha256}",
            )
        )
        return stored


class ExecuteToolTask:
    """The only application path from an execution-ready Staff Core task to a registered tool."""

    def __init__(
        self,
        tasks: TaskRepository,
        staff: StaffRepository,
        intents: ToolIntentRepository,
        registry: ToolRegistryPort,
        capabilities: CapabilityAuthorityPort,
        idempotency: IdempotencyRepository,
        clock: ClockPort,
        audit: AuditPort,
        *,
        capability_ttl_seconds: int = 60,
    ) -> None:
        if capability_ttl_seconds <= 0 or capability_ttl_seconds > 300:
            raise ValueError("Capability TTL must be between 1 and 300 seconds.")
        self._tasks = tasks
        self._staff = staff
        self._intents = intents
        self._registry = registry
        self._capabilities = capabilities
        self._idempotency = idempotency
        self._clock = clock
        self._audit = audit
        self._ttl = capability_ttl_seconds

    def _validate(self, task: Task, actor_id: str, intent: ToolExecutionIntent) -> ToolDefinition:
        actor = self._staff.get(actor_id)
        if task.assignee_id != actor_id:
            raise PermissionDenied("Only the assigned staff member may request gateway execution.")
        actor.assert_allowed(task.action, task.resource, task.risk)
        task.assert_ready_for_execution()
        if intent.task_id != task.task_id or intent.actor_id != actor_id:
            raise PermissionDenied("Stored tool intent does not belong to this task and assignee.")

        definition = self._registry.definition(intent.tool_id)
        PrepareToolExecution._validate_binding(task, definition, intent.operation)
        operation = definition.operation(intent.operation)

        if operation.mutating:
            if task.approval is None or task.approval.outcome is not ApprovalOutcome.APPROVED:
                raise PermissionDenied("Mutating tool operations require an explicit approved task.")
            if intent.prepared_at > task.approval.decided_at:
                raise PermissionDenied(
                    "Mutating tool intent was prepared after approval; re-review and re-approval are required."
                )
        return definition

    @staticmethod
    def _same_claims(actual: CapabilityClaims, expected: CapabilityClaims) -> bool:
        return (
            actual.task_id == expected.task_id
            and actual.actor_id == expected.actor_id
            and actual.tool_id == expected.tool_id
            and actual.operation == expected.operation
            and actual.arguments_sha256 == expected.arguments_sha256
            and actual.idempotency_key == expected.idempotency_key
        )

    def __call__(self, task_id: str, actor_id: str) -> Task:
        task = self._tasks.get(task_id)
        intent = self._intents.get(task_id)
        self._validate(task, actor_id, intent)

        idempotency_key = task.execution_key
        existing = self._idempotency.get(idempotency_key)
        if existing is not None:
            if existing.fingerprint != intent.fingerprint:
                raise ToolGatewayError("Idempotency key is bound to a different reviewed execution intent.")
            if existing.state is IdempotencyState.PROCESSING:
                raise ToolGatewayError(
                    "A prior tool execution is still processing or ambiguous; reconcile it before retrying."
                )
            if existing.receipt is None:
                raise ToolGatewayError("Completed idempotency record is missing its receipt.")
            updated = task.record_execution(existing.receipt.reference)
            self._tasks.save(updated)
            self._audit.append(
                AuditEvent(
                    "tool.execution_recovered",
                    task_id,
                    actor_id,
                    self._clock.now(),
                    existing.receipt.reference,
                )
            )
            return updated

        now = self._clock.now()
        claims = CapabilityClaims(
            task_id=task.task_id,
            actor_id=actor_id,
            tool_id=intent.tool_id,
            operation=intent.operation,
            arguments_sha256=intent.arguments_sha256,
            idempotency_key=idempotency_key,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
        )
        token = self._capabilities.issue(claims)
        verified = self._capabilities.verify(token, now=now)
        if not self._same_claims(verified, claims):
            raise ToolGatewayError("Capability authority returned claims that do not match the authorized execution.")

        self._idempotency.reserve(idempotency_key, intent.fingerprint, at=now)
        self._audit.append(
            AuditEvent(
                "tool.execution_dispatched",
                task_id,
                actor_id,
                now,
                f"{intent.tool_id}:{intent.operation}:{intent.arguments_sha256}",
            )
        )

        adapter = self._registry.adapter(intent.tool_id)
        try:
            receipt = adapter.execute(
                intent.operation,
                intent.arguments(),
                capability_token=token,
                idempotency_key=idempotency_key,
            )
        except ToolInvocationError as exc:
            if not exc.side_effect_started:
                self._idempotency.release_safe_failure(idempotency_key, intent.fingerprint)
            event = "tool.execution_safe_failure" if not exc.side_effect_started else "tool.execution_ambiguous"
            self._audit.append(AuditEvent(event, task_id, actor_id, self._clock.now(), str(exc)))
            raise
        except Exception as exc:
            # Unknown adapter failures are intentionally treated as ambiguous. The PROCESSING
            # reservation remains so an automatic retry cannot duplicate an external write.
            self._audit.append(
                AuditEvent("tool.execution_ambiguous", task_id, actor_id, self._clock.now(), type(exc).__name__)
            )
            raise

        if receipt.tool_id != intent.tool_id or receipt.operation != intent.operation:
            raise ToolGatewayError("Tool adapter returned a receipt for a different tool or operation.")

        self._idempotency.complete(idempotency_key, intent.fingerprint, receipt)
        updated = task.record_execution(receipt.reference)
        self._tasks.save(updated)
        self._audit.append(
            AuditEvent(
                "task.executed_via_gateway",
                task_id,
                actor_id,
                self._clock.now(),
                f"{receipt.reference}: {receipt.summary}",
            )
        )
        return updated
