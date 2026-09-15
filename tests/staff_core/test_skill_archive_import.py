from __future__ import annotations

import io
import zipfile

import pytest

from nemotron.staff.adapters.safe_skill_archives import ArchiveSafetyLimits, SafeSkillArchiveInspector


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _skill(name: str, description: str, body: str = "Follow the approved procedure.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


def test_zip_discovers_multiple_skills_independently() -> None:
    payload = _zip(
        {
            "skills/accounting/SKILL.md": _skill("Accounting Review", "Review accounting evidence"),
            "skills/odoo/SKILL.md": _skill("Odoo Operator", "Use Odoo safely"),
            "skills/odoo/helper.py": "raise RuntimeError('must never execute')\n",
        }
    )

    inspection = SafeSkillArchiveInspector().inspect("github-skills.zip", payload)

    assert inspection.rejected is False
    assert inspection.archive_kind == "zip"
    assert inspection.package_sha256
    assert [skill.name for skill in inspection.skills] == ["Accounting Review", "Odoo Operator"]
    assert {skill.source_path for skill in inspection.skills} == {
        "skills/accounting/SKILL.md",
        "skills/odoo/SKILL.md",
    }


def test_zip_rejects_path_traversal() -> None:
    payload = _zip({"../escape/SKILL.md": _skill("Escape", "Unsafe")})

    inspection = SafeSkillArchiveInspector().inspect("unsafe.zip", payload)

    assert inspection.rejected is True
    assert "traversal" in inspection.reason.lower()
    assert inspection.skills == ()


def test_zip_rejects_uncompressed_archive_bomb_limit() -> None:
    payload = _zip({"skill/SKILL.md": _skill("Big", "Large") + ("x" * 2_000)})
    inspector = SafeSkillArchiveInspector(
        limits=ArchiveSafetyLimits(max_total_uncompressed_bytes=500, max_file_bytes=4_000)
    )

    inspection = inspector.inspect("too-big.zip", payload)

    assert inspection.rejected is True
    assert "uncompressed" in inspection.reason.lower()


def test_skill_requires_yaml_frontmatter_name_and_description() -> None:
    payload = _zip({"skill/SKILL.md": "# no frontmatter\nDo work safely."})

    inspection = SafeSkillArchiveInspector().inspect("invalid.zip", payload)

    assert inspection.rejected is True
    assert "frontmatter" in inspection.reason.lower()


def test_rar_is_a_bounded_port_not_shell_execution() -> None:
    inspection = SafeSkillArchiveInspector().inspect("skills.rar", b"Rar!\x1a\x07\x01dummy")

    assert inspection.archive_kind == "rar"
    assert inspection.rejected is True
    assert "unsupported" in inspection.reason.lower()
