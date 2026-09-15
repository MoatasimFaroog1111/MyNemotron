from __future__ import annotations

import io
import zipfile

from nemotron.staff.adapters.safe_skill_archives import ArchiveSafetyLimits, SafeSkillArchiveInspector


def _zip(entries: dict[str, str | bytes]) -> bytes:
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


def test_skill_manifest_size_is_bounded_before_prompt_ingestion() -> None:
    payload = _zip({"skill/SKILL.md": _skill("Too Verbose", "Must be bounded", "x" * 1_000)})
    inspector = SafeSkillArchiveInspector(
        limits=ArchiveSafetyLimits(max_file_bytes=4_000, max_skill_manifest_bytes=256)
    )

    inspection = inspector.inspect("too-verbose.zip", payload)

    assert inspection.rejected is True
    assert "skill.md" in inspection.reason.lower()
    assert "size" in inspection.reason.lower()


def test_skill_requires_yaml_frontmatter_name_and_description() -> None:
    payload = _zip({"skill/SKILL.md": "# no frontmatter\nDo work safely."})

    inspection = SafeSkillArchiveInspector().inspect("invalid.zip", payload)

    assert inspection.rejected is True
    assert "frontmatter" in inspection.reason.lower()


def test_nested_zip_discovers_skill_without_executing_resources() -> None:
    inner = _zip(
        {
            "skill/SKILL.md": _skill("Nested Accounting", "Review nested accounting evidence"),
            "skill/helper.py": "raise RuntimeError('must never execute')\n",
        }
    )
    outer = _zip({"github-download/nested-skills.zip": inner})

    inspection = SafeSkillArchiveInspector().inspect("download.zip", outer)

    assert inspection.rejected is False
    assert [skill.name for skill in inspection.skills] == ["Nested Accounting"]
    assert "nested-skills.zip!/skill/SKILL.md" in inspection.skills[0].source_path


def test_nested_archive_depth_limit_fails_closed() -> None:
    deepest = _zip({"skill/SKILL.md": _skill("Too Deep", "Must be rejected")})
    middle = _zip({"inner.zip": deepest})
    outer = _zip({"middle.zip": middle})
    inspector = SafeSkillArchiveInspector(limits=ArchiveSafetyLimits(max_nested_depth=1))

    inspection = inspector.inspect("too-deep.zip", outer)

    assert inspection.rejected is True
    assert "depth" in inspection.reason.lower()


class _FakeRarReader:
    def read_files(self, payload: bytes, *, limits: ArchiveSafetyLimits) -> dict[str, bytes]:
        assert payload.startswith(b"Rar!\x1a\x07")
        assert limits.max_file_bytes > 0
        return {
            "skills/rar/SKILL.md": _skill("RAR Accounting", "Review accounting from RAR packages").encode(),
            "skills/rar/inert.py": b"raise RuntimeError('must never execute')\n",
        }


def test_rar_uses_bounded_reader_and_discovers_skill_as_inert_data() -> None:
    inspector = SafeSkillArchiveInspector(rar_reader=_FakeRarReader())

    inspection = inspector.inspect("skills.rar", b"Rar!\x1a\x07\x01\x00dummy")

    assert inspection.archive_kind == "rar"
    assert inspection.rejected is False
    assert [skill.name for skill in inspection.skills] == ["RAR Accounting"]
    assert inspection.skills[0].resources == ("skills/rar/inert.py",)
