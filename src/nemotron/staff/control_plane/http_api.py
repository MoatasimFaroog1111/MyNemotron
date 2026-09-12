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

from .security import SlidingWindowRateLimiter
from .service import ControlPlaneService


_MAX_BODY_BYTES = 1_000_000


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ControlPlaneService, api_token: str) -> None:
        super().__init__(address, ControlPlaneRequestHandler)
        self.service = service
        self.api_token = api_token
        self.rate_limiter = SlidingWindowRateLimiter()


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Deliberately omit headers/body; reverse proxy captures only safe request metadata.
        super().log_message(format, *args)

    @property
    def _config(self):  # type: ignore[no-untyped-def]
        return self.server.service.runtime.config

    def _write_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _client_key(self) -> str:
        return str(self.client_address[0])

    def _rate_limit(self, bucket: str, *, limit: int) -> bool:
        decision = self.server.rate_limiter.allow(bucket, self._client_key(), limit=limit)
        if decision.allowed:
            return True
        self._write_json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "rate_limited"},
            extra_headers={"Retry-After": str(decision.retry_after_seconds)},
        )
        return False

    def _host_allowed(self) -> bool:
        allowed = {value.lower() for value in self._config.allowed_hosts}
        if not allowed:
            return True
        raw = self.headers.get("Host", "").strip().lower()
        host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        return host in allowed

    def _transport_allowed(self) -> bool:
        if not self._config.require_forwarded_https:
            return True
        return self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        supplied = header[len(prefix) :]
        return secrets.compare_digest(supplied, self.server.api_token)

    def _require_auth(self) -> bool:
        if not self._host_allowed():
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_host"})
            return False
        if not self._transport_allowed():
            self._write_json(HTTPStatus.UPGRADE_REQUIRED, {"error": "https_required"})
            return False
        if self._authorized():
            return True
        if not self._rate_limit("auth-failure", limit=self._config.api_auth_failure_rpm):
            return False
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
        if length:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json.")
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
            self._write_json(HTTPStatus.FORBIDDEN, {"error": "permission_denied"})
        elif isinstance(exc, (InvalidTransition, ToolGatewayError, StaffRuntimeError, ToolInvocationError)):
            self._write_json(HTTPStatus.CONFLICT, {"error": type(exc).__name__})
        elif isinstance(exc, (ValueError, StaffCoreError)):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": type(exc).__name__})
        else:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                health = self.server.service.health()
                status = HTTPStatus.OK if health.get("ready") else HTTPStatus.SERVICE_UNAVAILABLE
                self._write_json(status, {"status": health.get("status"), "ready": health.get("ready")})
                return
            if parsed.path == "/ready":
                readiness = self.server.service.readiness()
                status = HTTPStatus.OK if readiness.get("ready") else HTTPStatus.SERVICE_UNAVAILABLE
                self._write_json(status, {"status": readiness.get("status"), "ready": readiness.get("ready")})
                return
            if not self._require_auth():
                return
            if not self._rate_limit("read", limit=self._config.api_read_rpm):
                return
            if parsed.path == "/api/v1/health":
                self._write_json(HTTPStatus.OK, self.server.service.readiness())
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
            if parsed.path == "/api/v1/backups":
                self._write_json(HTTPStatus.OK, {"items": self.server.service.backups()})
                return
            if parsed.path.startswith("/api/v1/tasks/"):
                task_id = parsed.path.removeprefix("/api/v1/tasks/")
                if "/" not in task_id and task_id:
                    self._write_json(HTTPStatus.OK, self.server.service.task(task_id))
                    return
            if parsed.path == "/api/v1/audit":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                if limit < 1 or limit > 500:
                    raise ValueError("limit must be between 1 and 500.")
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
        if not self._rate_limit("write", limit=self._config.api_write_rpm):
            return
        try:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if parsed.path == "/api/v1/backups":
                result = self.server.service.create_backup(
                    label=str(payload.get("label", "manual")),
                    actor_id=str(payload.get("actor_id", "control-plane")),
                )
                self._write_json(HTTPStatus.OK, result)
                return
            if parsed.path.startswith("/api/v1/backups/") and parsed.path.endswith("/restore"):
                name = parsed.path[len("/api/v1/backups/") : -len("/restore")].strip("/")
                result = self.server.service.restore_backup(
                    name,
                    recovery_token=self.headers.get("X-Recovery-Token", ""),
                    confirm=str(payload.get("confirm", "")),
                    actor_id=str(payload.get("actor_id", "recovery-admin")),
                )
                self._write_json(HTTPStatus.OK, result)
                return
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

    def _method_not_allowed(self) -> None:
        self._write_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def serve(service: ControlPlaneService, *, host: str, port: int, api_token: str) -> None:
    server = ControlPlaneHTTPServer((host, port), service, api_token)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
