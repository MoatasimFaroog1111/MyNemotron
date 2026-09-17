from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nemotron.staff.adapters.governed_staff_evaluation import (
    GovernedStaffEvaluationRunner,
    IsolatedEvaluationRuntimeFactory,
)
from nemotron.staff.application.queue_staff_instruction import QueueStaffInstructionRequest
from nemotron.staff.application.worker_ports import (
    WorkerAnalysis,
    WorkerAnalysisStatus,
    WorkerContext,
)
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.default_staff import (
    DEFAULT_ORGANIZATION_ID,
    ensure_default_staff_roster,
)
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.domain.runtime import MemoryEntry, MemoryScope
from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    StaffEvaluationCase,
    StaffEvaluationRubric,
)


class _ReadyReasoner:
    def analyze(self, context: WorkerContext) -> WorkerAnalysis:
        current_ref = f"ui-instruction:{context.work_item.work_item_id}"
        current = next(
            entry for entry in context.visible_memory if entry.source_reference == current_ref
        )
        return WorkerAnalysis(
            status=WorkerAnalysisStatus.READY,
            work_summary="Evaluation evidence is sufficient.",
            evidence_memory_ids=(current.memory_id,),
            decision_rationale="النتيجة 42 وتمت مراجعة الطلب ضمن النطاق المصرح.",
        )


class _NemotronStubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        context = json.loads(payload["messages"][1]["content"])
        current_ref = f"ui-instruction:{context['work_item']['work_item_id']}"
        memory = next(
            entry
            for entry in context["visible_memory"]
            if entry.get("source_reference") == current_ref
        )
        content = json.dumps(
            {
                "status": "ready",
                "work_summary": "تمت مراجعة الطلب داخل المسار المحكوم.",
                "evidence_memory_ids": [memory["memory_id"]],
                "decision_rationale": "النتيجة 42 ضمن النطاق المصرح دون تنفيذ خارجي.",
                "block_reason": None,
            },
            ensure_ascii=False,
        )
        body = json.dumps(
            {"choices": [{"message": {"content": content}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_model_stub() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NemotronStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _config(tmp_path, *, base_url: str = "https://model.example.test") -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "production" / "data",
        files_root=tmp_path / "production" / "files",
        host="127.0.0.1",
        port=8088,
        api_token="evaluation-control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"e" * 32,
        nemotron_base_url=base_url,
        nemotron_model="nemotron-evaluation-test",
        nemotron_api_key="model-secret",
        github_token="github-secret",
        github_allowed_repositories=("owner/repo",),
        smtp_host="smtp.example.test",
        smtp_username="user",
        smtp_password="smtp-secret",
        smtp_from_address="agent@example.test",
        smtp_allowed_domains=("example.test",),
        odoo_base_url="https://odoo.example.test",
        odoo_database="db",
        odoo_uid=7,
        odoo_api_key="odoo-secret",
        odoo_allowed_models=("account.move.line",),
        browser_allowed_hosts=("example.test",),
        worker_retry_initial_seconds=5,
        worker_retry_max_seconds=30,
    )


def _case(*, failure_kind: str | None = None) -> StaffEvaluationCase:
    return StaffEvaluationCase(
        case_id=f"staff-data-analyst:correctness:governed-{failure_kind or 'ready'}",
        staff_id="staff-data-analyst",
        category=(
            EvaluationCategory.RECOVERY
            if failure_kind is not None
            else EvaluationCategory.CORRECTNESS
        ),
        language="ar",
        instruction="احسب 20 + 22 وأجب بالنتيجة.",
        expected={},
        rubric=StaffEvaluationRubric(
            required_substrings=("42",),
            must_block=False,
            expected_language="ar",
            expected_task_states=("ready_for_execution",),
        ),
        risk_profile="low",
        inject_failure=failure_kind is not None,
        failure_kind=failure_kind,
        tags=("governed-runner",),
    )


def test_isolated_factory_never_mutates_production_state_or_files(tmp_path) -> None:
    base = _config(tmp_path)
    production = build_production_runtime(base, reasoner=_ReadyReasoner())
    ensure_default_staff_roster(production.staff, production.organizations)
    production.memories.save(
        MemoryEntry(
            memory_id="production-sentinel-memory",
            organization_id=DEFAULT_ORGANIZATION_ID,
            owner_staff_id="staff-data-analyst",
            scope=MemoryScope.PRIVATE,
            content="PRODUCTION SENTINEL MEMORY",
            created_at=production.clock.now(),
            source_reference="production:sentinel",
        )
    )
    production.queue_staff_instruction(
        QueueStaffInstructionRequest(
            staff_id="staff-data-analyst",
            instruction="PRODUCTION SENTINEL WORK ITEM",
            actor_id="production-test",
        )
    )
    base.files_root.mkdir(parents=True, exist_ok=True)
    sentinel_file = base.files_root / "sentinel.txt"
    sentinel_file.write_bytes(b"PRODUCTION-SENTINEL-FILE")

    before_memories = production.memories.list_for_organization(DEFAULT_ORGANIZATION_ID)
    before_inbox = production.worker_queue.inbox("staff-data-analyst")
    before_file = sentinel_file.read_bytes()

    factory = IsolatedEvaluationRuntimeFactory(
        base,
        reasoner=_ReadyReasoner(),
        evaluation_root=tmp_path / "evaluation",
    )
    outcome = GovernedStaffEvaluationRunner(factory).run(_case())

    assert outcome.final_task_state == "ready_for_execution"
    assert outcome.blocked is False
    assert outcome.execution_references == ()
    assert production.memories.list_for_organization(DEFAULT_ORGANIZATION_ID) == before_memories
    assert production.worker_queue.inbox("staff-data-analyst") == before_inbox
    assert sentinel_file.read_bytes() == before_file

    evaluation = factory.last_runtime
    assert evaluation is not None
    assert evaluation.config.data_dir.is_relative_to(tmp_path / "evaluation")
    assert evaluation.config.files_root.is_relative_to(tmp_path / "evaluation")
    assert evaluation.registered_tools == ("files",)
    for task_view in evaluation.queries.execution_dashboard():
        with pytest.raises(LookupError):
            production.tasks.get(task_view.task_id)


def test_live_runner_uses_real_governed_queue_worker_evidence_and_decision_path(tmp_path) -> None:
    server, thread, base_url = _start_model_stub()
    try:
        factory = IsolatedEvaluationRuntimeFactory(
            _config(tmp_path, base_url=base_url),
            evaluation_root=tmp_path / "evaluation-http",
        )
        outcome = GovernedStaffEvaluationRunner(factory).run(_case())
        runtime = factory.last_runtime
        assert runtime is not None

        event_types = {event.event_type for event in runtime.audit.list_recent(limit=100)}
        assert {
            "ui.instruction_submitted",
            "ui.instruction_memory_recorded",
            "worker.work_claimed",
            "worker.task_materialized",
            "task.evidence_recorded",
            "task.decision_recorded",
            "worker.decision_handoff",
        }.issubset(event_types)
        assert outcome.final_task_state == "ready_for_execution"
        assert outcome.decision_text is not None and "42" in outcome.decision_text
        assert outcome.evidence_references
        assert outcome.execution_references == ()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "failure_kind",
    ("reasoner_timeout_once", "repository_transient_once"),
)
def test_controlled_transient_faults_recover_without_duplicate_tasks(tmp_path, failure_kind: str) -> None:
    factory = IsolatedEvaluationRuntimeFactory(
        _config(tmp_path),
        reasoner=_ReadyReasoner(),
        evaluation_root=tmp_path / f"evaluation-{failure_kind}",
    )
    outcome = GovernedStaffEvaluationRunner(factory).run(_case(failure_kind=failure_kind))
    runtime = factory.last_runtime
    assert runtime is not None

    assert outcome.attempts == 2
    assert outcome.recovered_after_failure is True
    assert outcome.final_task_state == "ready_for_execution"
    dashboard = runtime.queries.execution_dashboard()
    assert len(dashboard) == 1
    assert runtime.worker_queue.inbox("staff-data-analyst") == ()
