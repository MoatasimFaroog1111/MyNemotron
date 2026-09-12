from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.e2e import run_governed_e2e
from nemotron.staff.control_plane.http_api import ControlPlaneHTTPServer
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService


class _NemotronStubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        system = payload["messages"][0]["content"]
        context = json.loads(payload["messages"][1]["content"])
        if "planning component" in system:
            department_id = context["goal"]["department_id"]
            content = json.dumps(
                {
                    "summary": "Governed one-step file plan",
                    "steps": [
                        {
                            "step_id": "write-output",
                            "title": "Write governed output",
                            "action": "write",
                            "resource": "files",
                            "risk": "medium",
                            "department_id": department_id,
                            "depends_on": [],
                        }
                    ],
                }
            )
        else:
            memory_id = context["visible_memory"][0]["memory_id"]
            content = json.dumps(
                {
                    "status": "ready",
                    "work_summary": "Trusted memory is sufficient for the authorized file write.",
                    "evidence_memory_ids": [memory_id],
                    "decision_rationale": "Execute only the already-authorized write using the reviewed evidence.",
                    "block_reason": None,
                }
            )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
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


def _config(tmp_path, base_url: str) -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"c" * 32,
        nemotron_base_url=base_url,
        nemotron_model="nemotron-e2e-stub",
    )


def test_governed_runtime_e2e_goal_to_nemotron_to_tool_to_verification(tmp_path) -> None:
    model_server, model_thread, base_url = _start_model_stub()
    try:
        runtime = build_production_runtime(_config(tmp_path, base_url))
        result = run_governed_e2e(runtime, prefix="ci")
        assert result.final_state == "completed"
        assert result.execution_reference == "ci/governed-output.txt"
        assert (tmp_path / "files" / "ci" / "governed-output.txt").read_text(encoding="utf-8") == (
            "MyNemotron governed E2E execution succeeded."
        )
        assert runtime.health()["ready"] is True
        dashboard = runtime.queries.execution_dashboard()
        assert any(item.task_id == result.task_id and item.state == "completed" for item in dashboard)
        assert result.audit_events >= 9
    finally:
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=2)


def test_http_control_plane_health_auth_and_audit(tmp_path) -> None:
    model_server, model_thread, base_url = _start_model_stub()
    api_server = None
    api_thread = None
    try:
        config = _config(tmp_path, base_url)
        runtime = build_production_runtime(config)
        result = run_governed_e2e(runtime, prefix="api")
        service = ControlPlaneService(runtime)
        api_server = ControlPlaneHTTPServer(("127.0.0.1", 0), service, config.api_token)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        host, port = api_server.server_address
        root = f"http://{host}:{port}"

        with urllib.request.urlopen(root + "/health", timeout=5) as response:
            health = json.load(response)
        assert health["ready"] is True

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(root + "/api/v1/audit", timeout=5)
        assert exc_info.value.code == 401

        request = urllib.request.Request(
            root + f"/api/v1/tasks/{result.task_id}",
            headers={"Authorization": f"Bearer {config.api_token}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            task = json.load(response)
        assert task["state"] == "completed"

        request = urllib.request.Request(
            root + "/api/v1/audit?limit=50",
            headers={"Authorization": f"Bearer {config.api_token}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            audit = json.load(response)
        assert any(item["event_type"] == "task.verified" for item in audit["items"])
    finally:
        if api_server is not None:
            api_server.shutdown()
            api_server.server_close()
        if api_thread is not None:
            api_thread.join(timeout=2)
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=2)


def test_config_summary_never_exposes_secrets(tmp_path) -> None:
    config = ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="super-secret-control-token-123456",
        capability_secret=b"z" * 32,
        nemotron_base_url="https://model.example.test",
        nemotron_model="nemotron",
        nemotron_api_key="nemotron-secret",
        github_token="github-secret",
        github_allowed_repositories=("owner/repo",),
    )
    rendered = json.dumps(config.redacted_summary())
    assert "super-secret-control-token" not in rendered
    assert "nemotron-secret" not in rendered
    assert "github-secret" not in rendered
