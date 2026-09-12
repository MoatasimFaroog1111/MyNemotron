from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nemotron.staff.adapters.capability_hmac import HMACCapabilityAuthority
from nemotron.staff.adapters.sqlite_gateway import (
    SQLiteGatewayStore,
    SQLiteIdempotencyRepository,
    SQLiteToolIntentRepository,
)
from nemotron.staff.adapters.tools.files import FilesToolAdapter, FilesToolConfig, files_tool_definition
from nemotron.staff.application.tool_gateway import (
    ExecuteToolTask,
    InMemoryToolRegistry,
    PrepareToolExecution,
    PrepareToolExecutionRequest,
)
from nemotron.staff.application.tool_ports import ToolInvocationError
from nemotron.staff.domain import (
    Approval,
    ApprovalOutcome,
    Decision,
    Evidence,
    Permission,
    PermissionDenied,
    RiskLevel,
    Role,
    StaffMember,
    Task,
    TaskState,
)
from nemotron.staff.domain.tools import (
    CapabilityClaims,
    CapabilityToken,
    ToolCategory,
    ToolDefinition,
    ToolExecutionReceipt,
    ToolGatewayError,
    ToolOperation,
    arguments_digest,
)


NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class Tasks:
    def __init__(self, task: Task) -> None:
        self.items = {task.task_id: task}

    def get(self, task_id: str) -> Task:
        try:
            return self.items[task_id]
        except KeyError as exc:
            raise LookupError(task_id) from exc

    def save(self, task: Task) -> None:
        self.items[task.task_id] = task


class Staff:
    def __init__(self, *members: StaffMember) -> None:
        self.items = {member.staff_id: member for member in members}

    def get(self, staff_id: str) -> StaffMember:
        try:
            return self.items[staff_id]
        except KeyError as exc:
            raise LookupError(staff_id) from exc

    def save(self, member: StaffMember) -> None:
        self.items[member.staff_id] = member

    def list_all(self) -> tuple[StaffMember, ...]:
        return tuple(self.items.values())


class Audit:
    def __init__(self) -> None:
        self.events = []

    def append(self, event) -> None:  # type: ignore[no-untyped-def]
        self.events.append(event)


class CountingAdapter:
    def __init__(self, authority: HMACCapabilityAuthority, *, fail_started: bool | None = None) -> None:
        self.authority = authority
        self.calls = 0
        self.fail_started = fail_started

    def execute(self, operation, arguments, *, capability_token, idempotency_key):  # type: ignore[no-untyped-def]
        self.calls += 1
        claims = self.authority.verify(capability_token, now=NOW)
        assert claims.operation == operation
        assert claims.idempotency_key == idempotency_key
        if self.fail_started is not None:
            raise ToolInvocationError("boom", side_effect_started=self.fail_started)
        return ToolExecutionReceipt.with_output(
            tool_id="github",
            operation=operation,
            reference="https://example.test/ref/1",
            summary="done",
            output={"ok": True, "arguments": dict(arguments)},
        )


def _worker(resource: str = "github", risk: RiskLevel = RiskLevel.MEDIUM) -> StaffMember:
    return StaffMember(
        "worker",
        "Worker",
        Role("role-worker", "Worker", (Permission("write", resource, risk), Permission("read", resource, risk))),
    )


def _awaiting_task(*, resource: str = "github", risk: RiskLevel = RiskLevel.MEDIUM, action: str = "write") -> Task:
    return Task(
        task_id="task-1",
        title="Governed external action",
        action=action,
        resource=resource,
        risk=risk,
        created_by="chief",
        created_at=NOW - timedelta(minutes=5),
        state=TaskState.AWAITING_APPROVAL if risk is not RiskLevel.LOW else TaskState.READY_FOR_EXECUTION,
        assignee_id="worker",
        evidence=(Evidence("memory", "source-1", "trusted fact", NOW - timedelta(minutes=4)),),
        decision=Decision(action, "perform reviewed action", NOW - timedelta(minutes=3)),
    )


def _approve(task: Task, at: datetime) -> Task:
    return replace(
        task,
        state=TaskState.READY_FOR_EXECUTION,
        approval=Approval("approver", ApprovalOutcome.APPROVED, "reviewed exact operation", at),
    )


def _github_definition() -> ToolDefinition:
    return ToolDefinition(
        "github",
        ToolCategory.GITHUB,
        "github",
        (ToolOperation("create_issue", ("write",), RiskLevel.MEDIUM, True),),
    )


def _gateway(tmp_path, *, adapter: CountingAdapter | None = None):  # type: ignore[no-untyped-def]
    task = _awaiting_task()
    tasks = Tasks(task)
    staff = Staff(_worker())
    audit = Audit()
    clock = Clock()
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    intents = SQLiteToolIntentRepository(store)
    idempotency = SQLiteIdempotencyRepository(store)
    authority = HMACCapabilityAuthority(b"a" * 32)
    actual_adapter = adapter or CountingAdapter(authority)
    registry = InMemoryToolRegistry()
    registry.register(_github_definition(), actual_adapter)
    prepare = PrepareToolExecution(tasks, staff, intents, registry, clock, audit)
    execute = ExecuteToolTask(tasks, staff, intents, registry, authority, idempotency, clock, audit)
    return tasks, audit, clock, intents, idempotency, authority, actual_adapter, prepare, execute


def test_mutating_execution_requires_intent_to_predate_approval(tmp_path) -> None:
    tasks, _, clock, _, _, _, adapter, prepare, execute = _gateway(tmp_path)
    prepare(
        PrepareToolExecutionRequest(
            "task-1",
            "worker",
            "github",
            "create_issue",
            {"owner": "acme", "repo": "app", "title": "Fix"},
        )
    )
    tasks.save(_approve(tasks.get("task-1"), clock.now() + timedelta(seconds=1)))
    result = execute("task-1", "worker")
    assert result.task.state is TaskState.VERIFYING
    assert result.receipt.output() == {"arguments": {"owner": "acme", "repo": "app", "title": "Fix"}, "ok": True}
    assert adapter.calls == 1


def test_intent_prepared_after_approval_is_rejected(tmp_path) -> None:
    tasks, _, clock, _, _, _, adapter, prepare, execute = _gateway(tmp_path)
    tasks.save(_approve(tasks.get("task-1"), clock.now() - timedelta(seconds=1)))
    prepare(
        PrepareToolExecutionRequest(
            "task-1",
            "worker",
            "github",
            "create_issue",
            {"owner": "acme", "repo": "app", "title": "Changed after approval"},
        )
    )
    with pytest.raises(PermissionDenied, match="after approval"):
        execute("task-1", "worker")
    assert adapter.calls == 0


def test_tool_intent_is_immutable_for_a_task(tmp_path) -> None:
    _, _, _, _, _, _, _, prepare, _ = _gateway(tmp_path)
    prepare(PrepareToolExecutionRequest("task-1", "worker", "github", "create_issue", {"title": "A"}))
    with pytest.raises(ToolGatewayError, match="different immutable"):
        prepare(PrepareToolExecutionRequest("task-1", "worker", "github", "create_issue", {"title": "B"}))


def test_ambiguous_side_effect_blocks_automatic_retry(tmp_path) -> None:
    authority = HMACCapabilityAuthority(b"a" * 32)
    adapter = CountingAdapter(authority, fail_started=True)
    tasks, _, clock, _, _, gateway_authority, _, prepare, execute = _gateway(tmp_path, adapter=adapter)
    adapter.authority = gateway_authority
    prepare(PrepareToolExecutionRequest("task-1", "worker", "github", "create_issue", {"title": "A"}))
    tasks.save(_approve(tasks.get("task-1"), clock.now() + timedelta(seconds=1)))
    with pytest.raises(ToolInvocationError):
        execute("task-1", "worker")
    with pytest.raises(ToolGatewayError, match="processing|ambiguous"):
        execute("task-1", "worker")
    assert adapter.calls == 1


def test_safe_failure_releases_idempotency_reservation(tmp_path) -> None:
    authority = HMACCapabilityAuthority(b"a" * 32)
    adapter = CountingAdapter(authority, fail_started=False)
    tasks, _, clock, _, idempotency, gateway_authority, _, prepare, execute = _gateway(tmp_path, adapter=adapter)
    adapter.authority = gateway_authority
    prepare(PrepareToolExecutionRequest("task-1", "worker", "github", "create_issue", {"title": "A"}))
    tasks.save(_approve(tasks.get("task-1"), clock.now() + timedelta(seconds=1)))
    with pytest.raises(ToolInvocationError):
        execute("task-1", "worker")
    assert idempotency.get("staff-task:task-1:execute") is None


def test_completed_receipt_recovers_without_calling_tool(tmp_path) -> None:
    tasks, _, clock, intents, idempotency, _, adapter, prepare, execute = _gateway(tmp_path)
    intent = prepare(PrepareToolExecutionRequest("task-1", "worker", "github", "create_issue", {"title": "A"}))
    tasks.save(_approve(tasks.get("task-1"), clock.now() + timedelta(seconds=1)))
    key = "staff-task:task-1:execute"
    idempotency.reserve(key, intent.fingerprint, at=clock.now())
    receipt = ToolExecutionReceipt.with_output(
        tool_id="github",
        operation="create_issue",
        reference="https://example.test/ref/recovered",
        summary="previously completed",
        output={"number": 9},
    )
    idempotency.complete(key, intent.fingerprint, receipt)
    result = execute("task-1", "worker")
    assert result.recovered is True
    assert result.receipt.output() == {"number": 9}
    assert adapter.calls == 0
    assert intents.get("task-1") == intent


def test_hmac_capability_rejects_tampering_and_expiry() -> None:
    authority = HMACCapabilityAuthority(b"b" * 32)
    claims = CapabilityClaims("t", "s", "files", "read_text", "a" * 64, "key", NOW, NOW + timedelta(seconds=10))
    token = authority.issue(claims)
    payload, signature = token.value.split(".", 1)
    tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    with pytest.raises(ToolGatewayError):
        authority.verify(CapabilityToken(f"{tampered_payload}.{signature}"), now=NOW)
    with pytest.raises(ToolGatewayError, match="expired"):
        authority.verify(token, now=NOW + timedelta(seconds=11))


def test_files_adapter_refuses_direct_invalid_capability_and_path_escape(tmp_path) -> None:
    authority = HMACCapabilityAuthority(b"c" * 32)
    adapter = FilesToolAdapter(FilesToolConfig(tmp_path / "root"), authority)
    with pytest.raises(ToolGatewayError):
        adapter.execute(
            "read_text",
            {"path": "note.txt"},
            capability_token=CapabilityToken("not.signed"),
            idempotency_key="k",
        )

    arguments = {"path": "../outside.txt"}
    issued = datetime.now(timezone.utc) - timedelta(seconds=1)
    claims = CapabilityClaims(
        "task-files",
        "worker",
        "files",
        "read_text",
        arguments_digest(arguments),
        "file-key",
        issued,
        issued + timedelta(seconds=30),
    )
    token = authority.issue(claims)
    with pytest.raises(ToolGatewayError, match="escapes"):
        adapter.execute("read_text", arguments, capability_token=token, idempotency_key="file-key")


def test_files_tool_definition_keeps_writes_behind_approval_risk() -> None:
    definition = files_tool_definition()
    assert definition.operation("read_text").mutating is False
    assert definition.operation("write_text").mutating is True
    assert definition.operation("write_text").minimum_risk is RiskLevel.MEDIUM
