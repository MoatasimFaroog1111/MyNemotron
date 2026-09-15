from __future__ import annotations

from . import http_api_legacy as legacy
from .sherman_skills import ShermanSkillControlService

_MAX_SKILL_UPLOAD_BYTES = 25 * 1024 * 1024
_SHERMAN_ASSETS = {
    "/ui/sherman.js": ("sherman.js", "text/javascript; charset=utf-8"),
    "/ui/sherman.css": ("sherman.css", "text/css; charset=utf-8"),
}


class ControlPlaneHTTPServer(legacy.ControlPlaneHTTPServer):
    """Existing control-plane server with isolated Sherman skill-training routes."""

    def __init__(
        self,
        address: tuple[str, int],
        service: legacy.ControlPlaneService,
        api_token: str,
    ) -> None:
        legacy.ThreadingHTTPServer.__init__(self, address, ControlPlaneRequestHandler)
        self.service = service
        self.api_token = api_token
        self.rate_limiter = legacy.SlidingWindowRateLimiter()
        self.ui_sessions = legacy.UISessionManager(service.runtime.config.capability_secret)
        self.frontend_root = legacy.Path(__file__).with_name("frontend")


class ControlPlaneRequestHandler(legacy.ControlPlaneRequestHandler):
    server: ControlPlaneHTTPServer

    def _skills(self) -> ShermanSkillControlService:
        return ShermanSkillControlService(self.server.service.runtime)

    def _serve_ui_asset(self, path: str) -> bool:
        sherman_asset = _SHERMAN_ASSETS.get(path)
        if sherman_asset is None:
            return super()._serve_ui_asset(path)
        filename, content_type = sherman_asset
        asset = self.server.frontend_root / filename
        if not asset.is_file():
            self._write_json(legacy.HTTPStatus.NOT_FOUND, {"error": "ui_asset_missing"})
            return True
        self._write_bytes(
            legacy.HTTPStatus.OK,
            asset.read_bytes(),
            content_type=content_type,
            cache_control="no-cache",
        )
        return True

    def _read_skill_payload(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 1:
            raise ValueError("Skill archive payload is required.")
        if length > _MAX_SKILL_UPLOAD_BYTES:
            raise ValueError("Skill archive upload is too large.")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/octet-stream", "application/zip", "application/x-zip-compressed"}:
            raise ValueError("Skill import requires a binary request body.")
        return self.rfile.read(length)

    def _handle_skill_get(self, path: str, *, ui: bool) -> bool:
        prefix = "/ui/api" if ui else "/api/v1"
        if path == f"{prefix}/skills":
            self._write_json(legacy.HTTPStatus.OK, {"items": self._skills().list_skills()})
            return True
        staff_prefix = f"{prefix}/staff/"
        if path.startswith(staff_prefix) and path.endswith("/skills"):
            staff_id = legacy.unquote(path[len(staff_prefix) : -len("/skills")].strip("/"))
            if not staff_id or "/" in staff_id:
                raise ValueError("Invalid staff id.")
            self._write_json(legacy.HTTPStatus.OK, {"items": self._skills().resolve_for_staff(staff_id)})
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = legacy.urlparse(self.path)
            if parsed.path.startswith("/ui/api/") and (
                parsed.path == "/ui/api/skills" or parsed.path.endswith("/skills")
            ):
                if not self._require_ui_auth():
                    return
                if not self._rate_limit("ui-read", limit=self._config.api_read_rpm):
                    return
                if self._handle_skill_get(parsed.path, ui=True):
                    return
            if parsed.path.startswith("/api/v1/") and (
                parsed.path == "/api/v1/skills" or parsed.path.endswith("/skills")
            ):
                if not self._require_auth():
                    return
                if not self._rate_limit("read", limit=self._config.api_read_rpm):
                    return
                if self._handle_skill_get(parsed.path, ui=False):
                    return
        except Exception as exc:
            self._handle_error(exc)
            return
        super().do_GET()

    def _handle_skill_post(self, parsed, *, ui: bool) -> bool:  # type: ignore[no-untyped-def]
        prefix = "/ui/api" if ui else "/api/v1"
        if parsed.path == f"{prefix}/skills/import":
            filename = legacy.parse_qs(parsed.query).get("filename", [""])[0]
            result = self._skills().import_archive(
                filename=filename,
                payload=self._read_skill_payload(),
                actor_id="staff-sherman-trainer" if ui else "control-plane",
            )
            self._write_json(legacy.HTTPStatus.CREATED, result)
            return True

        skill_prefix = f"{prefix}/skills/"
        if not parsed.path.startswith(skill_prefix):
            return False
        rest = parsed.path.removeprefix(skill_prefix)
        version_id, separator, action = rest.partition("/")
        if not separator or not version_id or action not in {"recommendations", "assignments", "deactivate"}:
            return False
        payload = self._read_json()
        actor_id = "staff-sherman-trainer" if ui else "control-plane"
        skills = self._skills()
        if action == "recommendations":
            result = skills.recommend(version_id, actor_id=actor_id)
        elif action == "assignments":
            raw_staff_ids = payload.get("staff_ids", [])
            if not isinstance(raw_staff_ids, list) or any(not isinstance(item, str) for item in raw_staff_ids):
                raise ValueError("staff_ids must be an array of strings.")
            result = skills.assign(
                version_id,
                mode=str(payload.get("mode", "selected")),
                actor_id=actor_id,
                staff_ids=tuple(raw_staff_ids),
            )
        else:
            result = skills.deactivate(version_id, actor_id=actor_id)
        self._write_json(legacy.HTTPStatus.OK, result)
        return True

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = legacy.urlparse(self.path)
            if parsed.path.startswith("/ui/api/skills"):
                if not self._require_ui_auth():
                    return
                if not self._request_origin_allowed():
                    self._write_json(legacy.HTTPStatus.FORBIDDEN, {"error": "origin_rejected"})
                    return
                if not self._rate_limit("ui-write", limit=self._config.api_write_rpm):
                    return
                if self._handle_skill_post(parsed, ui=True):
                    return
            if parsed.path.startswith("/api/v1/skills"):
                if not self._require_auth():
                    return
                if not self._rate_limit("write", limit=self._config.api_write_rpm):
                    return
                if self._handle_skill_post(parsed, ui=False):
                    return
        except Exception as exc:
            self._handle_error(exc)
            return
        super().do_POST()


def serve(service: legacy.ControlPlaneService, *, host: str, port: int, api_token: str) -> None:
    server = ControlPlaneHTTPServer((host, port), service, api_token)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
