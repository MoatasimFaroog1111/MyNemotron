from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.default_staff import ensure_default_staff_roster
from nemotron.staff.control_plane.http_api import ControlPlaneHTTPServer
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService
from nemotron.staff.control_plane.sherman_skills import ShermanSkillControlService


def _config(tmp_path) -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"s" * 32,
        nemotron_base_url="https://model.example.test",
        nemotron_model="nemotron-test",
    )


def _skills_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "skills/accounting/SKILL.md",
            "---\nname: Odoo Accounting Review\ndescription: Review Odoo accounting and bank reconciliation evidence.\n---\nUse evidence first.\n",
        )
        archive.writestr(
            "skills/development/SKILL.md",
            "---\nname: Python API Development\ndescription: Develop Python backend API integrations safely.\n---\nWrite tests first.\n",
        )
        archive.writestr("skills/accounting/inert.py", "raise RuntimeError('must never execute')\n")
    return buffer.getvalue()


def _json_request(url: str, token: str, payload: dict[str, object]) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )


def test_authenticated_skill_import_recommend_and_assign_is_explicit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path)
    runtime = build_production_runtime(config)
    ensure_default_staff_roster(runtime.staff, runtime.organizations)
    skills = ShermanSkillControlService(runtime)
    server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneService(runtime), config.api_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        upload_url = f"http://{host}:{port}/api/v1/skills/import?filename=github-skills.zip"

        unauthenticated = urllib.request.Request(
            upload_url,
            data=_skills_zip(),
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            urllib.request.urlopen(unauthenticated, timeout=5)
            raise AssertionError("Unauthenticated skill upload must not succeed.")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        request = urllib.request.Request(
            upload_url,
            data=_skills_zip(),
            headers={
                "Authorization": f"Bearer {config.api_token}",
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            imported = json.load(response)

        assert imported["package_sha256"]
        assert len(imported["skills"]) == 2
        accounting = next(item for item in imported["skills"] if item["skill_id"] == "odoo-accounting-review")
        version_id = accounting["version_id"]
        assert accounting["status"] == "pending"
        assert skills.resolve_for_staff("staff-bank-reconciliation") == []

        recommend = _json_request(
            f"http://{host}:{port}/api/v1/skills/{version_id}/recommendations",
            config.api_token,
            {"actor_id": "ui-operator"},
        )
        with urllib.request.urlopen(recommend, timeout=5) as response:
            recommendation = json.load(response)
        assert recommendation["staff_ids"]
        assert "staff-bank-reconciliation" in recommendation["staff_ids"]
        target_staff_id = recommendation["staff_ids"][0]
        assert recommendation["activated"] is False
        assert skills.resolve_for_staff(target_staff_id) == []

        assign = _json_request(
            f"http://{host}:{port}/api/v1/skills/{version_id}/assignments",
            config.api_token,
            {"mode": "suggested", "actor_id": "ui-operator"},
        )
        with urllib.request.urlopen(assign, timeout=5) as response:
            assignment = json.load(response)

        assert assignment["activated"] is True
        assert target_staff_id in assignment["staff_ids"]
        resolved = skills.resolve_for_staff(target_staff_id)
        assert len(resolved) == 1
        assert resolved[0]["version_id"] == version_id
        assert any(event.event_type == "skill.imported" for event in runtime.audit.list_recent(limit=100))
        assert any(event.event_type == "skill.training_assigned" for event in runtime.audit.list_recent(limit=100))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
