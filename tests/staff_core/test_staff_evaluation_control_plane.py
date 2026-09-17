from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.http_api import ControlPlaneHTTPServer
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService
from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationRunMode,
    EvaluationSuiteIdentity,
    ReadinessFailure,
    ReadinessResult,
    StaffCaseScore,
    StaffEvaluationReport,
)


def _config(tmp_path) -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"e" * 32,
        nemotron_base_url="http://127.0.0.1:9",
        nemotron_model="nemotron-control-plane-test",
    )


def _seed_report(runtime) -> StaffEvaluationReport:  # type: ignore[no-untyped-def]
    score = StaffCaseScore(
        case_id="staff-financial-accountant:correctness:test-001",
        category=EvaluationCategory.CORRECTNESS,
        passed=True,
        correctness=1.0,
        safety=None,
        recovery=None,
        language_compliance=None,
        latency_ms=125.0,
        cost_usd=None,
        failure_reasons=(),
    )
    report = StaffEvaluationReport(
        report_id="report-financial-accountant-001",
        office_run_id="office-run-001",
        staff_id="staff-financial-accountant",
        suite=EvaluationSuiteIdentity(suite_id="gold-v1", dataset_digest="a" * 64),
        mode=EvaluationRunMode.LIVE,
        model_id="nemotron-control-plane-test",
        config_digest="b" * 64,
        git_sha="c" * 40,
        scores=(score,),
        sample_size=1,
        correctness_rate=1.0,
        safety_pass_rate=None,
        recovery_rate=None,
        blocked_rate=0.0,
        average_latency_ms=125.0,
        p50_latency_ms=125.0,
        p95_latency_ms=125.0,
        average_attempts=1.0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        usage_measured_cases=0,
        average_cost_usd=None,
        cost_measured_cases=0,
        failed_case_ids=(),
        governance_violations=(),
        readiness=ReadinessResult(
            ready=False,
            status=ReadinessFailure.INSUFFICIENT_EVIDENCE.value,
            failures=(ReadinessFailure.INSUFFICIENT_EVIDENCE,),
        ),
        measured_at=datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
    )
    runtime.evaluation_report_store.append_staff(report)
    return report


def _start_api(tmp_path):  # type: ignore[no-untyped-def]
    config = _config(tmp_path)
    runtime = build_production_runtime(config)
    report = _seed_report(runtime)
    server = ControlPlaneHTTPServer(
        ("127.0.0.1", 0),
        ControlPlaneService(runtime),
        config.api_token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return config, runtime, report, server, thread, f"http://{host}:{port}"


def _authorized_request(url: str, token: str, *, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_evaluation_reads_require_auth_and_list_persisted_metadata(tmp_path) -> None:
    config, _runtime, report, server, thread, root = _start_api(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(root + "/api/v1/evaluations/staff", timeout=5)
        assert exc_info.value.code == 401

        request = _authorized_request(root + "/api/v1/evaluations/staff", config.api_token)
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload["items"][0]["report_id"] == report.report_id
        assert payload["items"][0]["staff_id"] == report.staff_id
        assert payload["items"][0]["status"] == "measured"
        assert "scores" not in payload["items"][0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_staff_without_persisted_report_is_explicitly_unmeasured(tmp_path) -> None:
    config, _runtime, _report, server, thread, root = _start_api(tmp_path)
    try:
        request = _authorized_request(
            root + "/api/v1/evaluations/staff/staff-never-measured",
            config.api_token,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload == {"staff_id": "staff-never-measured", "status": "unmeasured"}
        assert "correctness_rate" not in payload
        assert "safety_pass_rate" not in payload
        assert "recovery_rate" not in payload
        assert "p95_latency_ms" not in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_immutable_report_endpoint_exposes_safe_observables_only(tmp_path) -> None:
    config, _runtime, report, server, thread, root = _start_api(tmp_path)
    try:
        request = _authorized_request(
            root + f"/api/v1/evaluations/reports/{report.report_id}",
            config.api_token,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload["report_id"] == report.report_id
        assert payload["scores"][0]["case_id"].endswith("test-001")
        rendered = json.dumps(payload, ensure_ascii=False).casefold()
        assert "instruction" not in rendered
        assert "provider_payload" not in rendered
        assert "messages" not in rendered
        assert "prompt_text" not in rendered
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_evaluation_routes_have_no_write_surface(tmp_path, method: str) -> None:  # type: ignore[no-untyped-def]
    config, _runtime, _report, server, thread, root = _start_api(tmp_path)
    try:
        request = _authorized_request(
            root + "/api/v1/evaluations/staff",
            config.api_token,
            method=method,
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code in {404, 405}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
