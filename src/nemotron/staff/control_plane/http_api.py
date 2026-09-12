from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from nemotron.staff.application.tool_ports import ToolInvocationError
from nemotron.staff.domain import InvalidTransition, PermissionDenied, StaffCoreError
from nemotron.staff.domain.runtime import RuntimeError as StaffRuntimeError
from nemotron.staff.domain.tools import ToolGatewayError

from .service import ControlPlaneService


_MAX_BODY_BYTES = 1_000_000


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ControlPlaneService, api_token: str) -> None:
        super().__init__(address, ControlPlaneRequestHandler)
        self.service = service
        self.api_token = api_token


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Deliberately omit headers/body; reverse proxy or structured logging can capture safe metadata.
        super().log_message(format, *args)

    def _write_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        supplied = header[len(prefix) :]
        return secrets.compare_digest(supplied, self.server.api_token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, LookupError):
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        elif isinstance(exc, PermissionDenied):
            self._write_json(HTTPStatus.FORBIDDEN, {"error": "permission_denied", "detail": str(exc)})
        elif isinstance(exc, (InvalidTransition, ToolGatewayError, StaffRuntimeError, ToolInvocationError)):
            self._write_json(HTTPStatus.CONFLICT, {"error": type(exc).__name__, "detail": str(exc)})
        elif isinstance(exc, (ValueError, StaffCoreError)):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__, "detail": str(exc)})
        else:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                health = self.server.service.health()
                status = HTTPStatus.OK if health.get("ready") else HTTPStatus.SERVICE_UNAVAILABLE
                self._write_json(status, health)
                return
            if not self._require_auth():
                return
            if parsed.path == "/api/v1/config":
                self._write_json(HTTPStatus.OK, self.server.service.config_summary())
                return
            if parsed.path == "/api/v1/approvals":
                self._write_json(HTTPStatus.OK, {"items": self.server.service.approval_inbox()})
                return
            if parsed.path == "/api/v1/executions":
                self._write_json(HTTPStatus.OK, {"items": self.server.service.execution_dashboard()})
                return
            if parsed.path.startswith("/api/v1/tasks/"):
                task_id = parsed.path.removeprefix("/api/v1/tasks/")
                if "/" not in task_id and task_id:
                    self._write_json(HTTPStatus.OK, self.server.service.task(task_id))
                    return
            if parsed.path == "/api/v1/audit":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                subject_type = query.get("subject_type", [None])[0]
                subject_id = query.get("subject_id", [None])[0]
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "items": self.server.service.audit_timeline(
                            limit=limit,
                            subject_type=subject_type,
                            subject_id=subject_id,
                        )
                    },
                )
                return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        try:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if parsed.path.startswith("/api/v1/workers/") and parsed.path.endswith("/run"):
                staff_id = parsed.path[len("/api/v1/workers/") : -len("/run")].strip("/")
                self._write_json(HTTPStatus.OK, self.server.service.run_worker(staff_id))
                return
            if parsed.path.startswith("/api/v1/tasks/"):
                rest = parsed.path.removeprefix("/api/v1/tasks/")
                task_id, separator, action = rest.partition("/")
                if not separator or not task_id:
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                if action == "intent":
                    arguments = payload.get("arguments")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object.")
                    result = self.server.service.prepare_tool_execution(
                        task_id,
                        actor_id=str(payload.get("actor_id", "")),
                        tool_id=str(payload.get("tool_id", "")),
                        operation=str(payload.get("operation", "")),
                        arguments=arguments,
                    )
                    self._write_json(HTTPStatus.OK, result)
                    return
                if action == "approval":
                    result = self.server.service.decide_approval(
                        task_id,
                        approver_id=str(payload.get("approver_id", "")),
                        approved=bool(payload.get("approved", False)),
                        rationale=str(payload.get("rationale", "")),
                    )
                    self._write_json(HTTPStatus.OK, result)
                    return
                if action == "execute":
                    result = self.server.service.execute(task_id, actor_id=str(payload.get("actor_id", "")))
                    self._write_json(HTTPStatus.OK, result)
                    return
                if action == "verify":
                    result = self.server.service.verify(
                        task_id,
                        verifier_id=str(payload.get("verifier_id", "")),
                        passed=bool(payload.get("passed", False)),
                        summary=str(payload.get("summary", "")),
                    )
                    self._write_json(HTTPStatus.OK, result)
                    return
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)


def serve(service: ControlPlaneService, *, host: str, port: int, api_token: str) -> None:
    server = ControlPlaneHTTPServer((host, port), service, api_token)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
