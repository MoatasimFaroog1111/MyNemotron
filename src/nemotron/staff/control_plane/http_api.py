from __future__ import annotations

import json
import mimetypes
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from nemotron.staff.application.tool_ports import ToolInvocationError
from nemotron.staff.domain import InvalidTransition, PermissionDenied, StaffCoreError
from nemotron.staff.domain.runtime import RuntimeError as StaffRuntimeError
from nemotron.staff.domain.tools import ToolGatewayError

from .security import SlidingWindowRateLimiter
from .service import ControlPlaneService
from .ui_session import UISessionManager


_MAX_BODY_BYTES = 1_000_000
_UI_ASSETS = {
    "/ui/": "index.html",
    "/ui/index.html": "index.html",
    "/ui/app.css": "app.css",
    "/ui/app.js": "app.js",
    "/ui/team-original.jpg": "team-original.jpg",
}


class ControlPlaneHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ControlPlaneService, api_token: str) -> None:
        super().__init__(address, ControlPlaneRequestHandler)
        self.service = service
        self.api_token = api_token
        self.rate_limiter = SlidingWindowRateLimiter()
        self.ui_sessions = UISessionManager(service.runtime.config.capability_secret)
        self.frontend_root = Path(__file__).with_name("frontend")


class ControlPlaneRequestHandler(BaseHTTPRequestHandler):
    server: ControlPlaneHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Deliberately omit headers/body; reverse proxy captures only safe request metadata.
        super().log_message(format, *args)

    @property
    def _config(self):  # type: ignore[no-untyped-def]
        return self.server.service.runtime.config

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if self._config.require_forwarded_https:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

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
        self._security_headers()
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _write_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        cache_control: str = "no-store",
        content_security_policy: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        if content_security_policy:
            self.send_header("Content-Security-Policy", content_security_policy)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND.value)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()

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

    def _request_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        request_host = self.headers.get("Host", "").strip().lower()
        return parsed.netloc.lower() == request_host

    def _require_transport(self) -> bool:
        if not self._host_allowed():
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_host"})
            return False
        if not self._transport_allowed():
            self._write_json(HTTPStatus.UPGRADE_REQUIRED, {"error": "https_required"})
            return False
        return True

    def _require_auth(self) -> bool:
        if not self._require_transport():
            return False
        if self._authorized():
            return True
        if not self._rate_limit("auth-failure", limit=self._config.api_auth_failure_rpm):
            return False
        self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _ui_authorized(self) -> bool:
        return self.server.ui_sessions.session_from_headers(self.headers) is not None

    def _require_ui_auth(self) -> bool:
        if not self._require_transport():
            return False
        if self._ui_authorized():
            return True
        if not self._rate_limit("ui-auth-failure", limit=self._config.api_auth_failure_rpm):
            return False
        self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "ui_session_required"})
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

    def _serve_ui_asset(self, path: str) -> bool:
        filename = _UI_ASSETS.get(path)
        if filename is None:
            return False
        asset = self.server.frontend_root / filename
        if not asset.is_file():
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "ui_asset_missing"})
            return True
        body = asset.read_bytes()
        if filename == "team-original.jpg" and (
            not body.startswith(b"\xff\xd8\xff") or not body.endswith(b"\xff\xd9")
        ):
            raise ValueError("Packaged team image failed JPEG signature validation.")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if filename.endswith(".html"):
            csp = (
                "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            cache = "no-store"
        elif filename.endswith((".js", ".css")):
            csp = None
            cache = "no-cache"
        else:
            csp = None
            cache = "public, max-age=86400"
        self._write_bytes(
            HTTPStatus.OK,
            body,
            content_type=content_type,
            cache_control=cache,
            content_security_policy=csp,
        )
        return True

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._redirect("/ui/")
                return
            if parsed.path == "/ui":
                self._redirect("/ui/")
                return
            if self._serve_ui_asset(parsed.path):
                return
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

            if parsed.path.startswith("/ui/api/"):
                if not self._require_ui_auth():
                    return
                if not self._rate_limit("ui-read", limit=self._config.api_read_rpm):
                    return
                if parsed.path == "/ui/api/session":
                    self._write_json(HTTPStatus.OK, {"authenticated": True})
                    return
                if parsed.path == "/ui/api/staff":
                    self._write_json(HTTPStatus.OK, {"items": self.server.service.staff_directory()})
                    return
                if parsed.path.startswith("/ui/api/staff/") and parsed.path.endswith("/workspace"):
                    staff_id = unquote(parsed.path[len("/ui/api/staff/") : -len("/workspace")].strip("/"))
                    if not staff_id or "/" in staff_id:
                        raise ValueError("Invalid staff id.")
                    self._write_json(HTTPStatus.OK, self.server.service.staff_workspace(staff_id))
                    return
                if parsed.path == "/ui/api/overview":
                    self._write_json(HTTPStatus.OK, self.server.service.ui_overview())
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
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
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/ui/login":
                if not self._require_transport():
                    return
                if not self._request_origin_allowed():
                    self._write_json(HTTPStatus.FORBIDDEN, {"error": "origin_rejected"})
                    return
                if not self._rate_limit("ui-login", limit=self._config.api_auth_failure_rpm):
                    return
                payload = self._read_json()
                token = str(payload.get("token", ""))
                if not token or not secrets.compare_digest(token, self.server.api_token):
                    self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials"})
                    return
                session_token = self.server.ui_sessions.issue()
                self._write_json(
                    HTTPStatus.OK,
                    {"authenticated": True},
                    extra_headers={"Set-Cookie": self.server.ui_sessions.set_cookie_header(session_token)},
                )
                return
            if parsed.path == "/ui/logout":
                if not self._require_transport():
                    return
                self._write_json(
                    HTTPStatus.OK,
                    {"authenticated": False},
                    extra_headers={"Set-Cookie": self.server.ui_sessions.clear_cookie_header()},
                )
                return

            if not self._require_auth():
                return
            if not self._rate_limit("write", limit=self._config.api_write_rpm):
                return
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
