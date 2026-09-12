from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.http_api import ControlPlaneHTTPServer
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService
from nemotron.staff.domain import Permission, RiskLevel, Role, StaffMember
from nemotron.staff.domain.organization import Department, Organization, StaffPlacement


def _config(tmp_path) -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="browser-control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"u" * 32,
        nemotron_base_url="https://model.example.test",
        nemotron_model="nemotron-ui-test",
    )


def _start_ui(tmp_path):  # type: ignore[no-untyped-def]
    config = _config(tmp_path)
    runtime = build_production_runtime(config)
    member = StaffMember(
        "staff-ui-1",
        "موظف الاختبار",
        Role(
            "role-ui-1",
            "محلل العمليات",
            (Permission("read", "github", RiskLevel.LOW),),
        ),
    )
    runtime.staff.save(member)
    runtime.organizations.save(
        Organization(
            organization_id="org-ui",
            name="منظمة الاختبار",
            departments=(Department("ops", "العمليات"),),
            placements=(StaffPlacement(member.staff_id, "ops", "محلل العمليات"),),
        )
    )
    server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneService(runtime), config.api_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return config, server, thread, f"http://{host}:{port}"


def _login(root: str, token: str) -> tuple[str, str]:
    request = urllib.request.Request(
        root + "/ui/login",
        data=json.dumps({"token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        set_cookie = response.headers["Set-Cookie"]
    return set_cookie.split(";", 1)[0], set_cookie


def test_official_frontend_is_same_origin_and_api_token_is_not_embedded(tmp_path) -> None:
    config, server, thread, root = _start_ui(tmp_path)
    try:
        with urllib.request.urlopen(root + "/ui/", timeout=5) as response:
            html = response.read().decode("utf-8")
            csp = response.headers["Content-Security-Policy"]
        assert "فريق العمل التفاعلي" in html
        assert "/ui/app.js" in html
        assert "/ui/team-original.jpg" in html
        assert config.api_token not in html
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

        with urllib.request.urlopen(root + "/ui/team-original.jpg", timeout=5) as response:
            image = response.read()
            assert response.headers["Content-Type"] == "image/jpeg"
        assert image.startswith(b"\xff\xd8\xff")
        assert len(image) > 100_000

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(root + "/ui/api/staff", timeout=5)
        assert exc_info.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ui_login_issues_http_only_session_and_projects_real_staff(tmp_path) -> None:
    config, server, thread, root = _start_ui(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _login(root, "wrong-token")
        assert exc_info.value.code == 401

        cookie, set_cookie = _login(root, config.api_token)
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert config.api_token not in set_cookie

        request = urllib.request.Request(root + "/ui/api/staff", headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=5) as response:
            staff = json.load(response)
        assert staff["items"] == [
            {
                "staff_id": "staff-ui-1",
                "display_name": "موظف الاختبار",
                "status": "active",
                "role_id": "role-ui-1",
                "role_name": "محلل العمليات",
                "approval_limit": None,
            }
        ]

        request = urllib.request.Request(
            root + "/ui/api/staff/staff-ui-1/workspace",
            headers={"Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            workspace = json.load(response)
        assert workspace["placement"]["department_name"] == "العمليات"
        assert workspace["permissions"][0]["resource"] == "github"
        assert workspace["tasks"] == []

        name, value = cookie.split("=", 1)
        tampered = f"{name}={value[:-1]}x"
        request = urllib.request.Request(root + "/ui/api/staff", headers={"Cookie": tampered})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ui_browser_session_cannot_call_write_control_plane_routes(tmp_path) -> None:
    config, server, thread, root = _start_ui(tmp_path)
    try:
        cookie, _ = _login(root, config.api_token)
        request = urllib.request.Request(
            root + "/api/v1/backups",
            data=b'{}',
            headers={"Cookie": cookie, "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 401

        request = urllib.request.Request(root + "/ui/api/overview", headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=5) as response:
            overview = json.load(response)
        assert "approvals" in overview
        assert "executions" in overview
        assert "audit" in overview
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
