from __future__ import annotations

import hashlib
import io
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from nemotron.staff.application.skill_ports import ArchiveInspection
from nemotron.staff.domain.skills import SkillDefinition


@dataclass(frozen=True, slots=True)
class ArchiveSafetyLimits:
    max_upload_bytes: int = 25 * 1024 * 1024
    max_total_uncompressed_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 2_000
    max_nested_depth: int = 2


class SafeSkillArchiveInspector:
    """Inspect hostile skill archives as inert data; never execute package content."""

    _RAR_MAGIC = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
    _SEVEN_Z_MAGIC = b"7z\xbc\xaf'\x1c"

    def __init__(self, *, limits: ArchiveSafetyLimits | None = None) -> None:
        self.limits = limits or ArchiveSafetyLimits()

    def inspect(self, filename: str, payload: bytes) -> ArchiveInspection:
        digest = hashlib.sha256(payload).hexdigest()
        kind = self._kind(payload)
        if len(payload) > self.limits.max_upload_bytes:
            return self._reject(filename, kind, digest, "compressed upload size exceeds safety limit")
        if kind == "rar":
            return self._reject(filename, kind, digest, "unsupported RAR backend; archive was not executed")
        if kind == "7z":
            return self._reject(filename, kind, digest, "unsupported 7z backend; archive was not executed")
        if kind == "zip":
            return self._inspect_zip(filename, payload, digest)
        if kind == "tar":
            return self._inspect_tar(filename, payload, digest)
        return self._reject(filename, kind, digest, "unsupported archive format")

    def _kind(self, payload: bytes) -> str:
        if payload.startswith(self._RAR_MAGIC):
            return "rar"
        if payload.startswith(self._SEVEN_Z_MAGIC):
            return "7z"
        if zipfile.is_zipfile(io.BytesIO(payload)):
            return "zip"
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
                return "tar"
        except tarfile.TarError:
            return "unknown"

    def _inspect_zip(self, filename: str, payload: bytes, digest: str) -> ArchiveInspection:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = archive.infolist()
                reason = self._validate_common_entries(
                    [(info.filename, info.file_size) for info in infos if not info.is_dir()]
                )
                if reason:
                    return self._reject(filename, "zip", digest, reason)
                for info in infos:
                    if info.flag_bits & 0x1:
                        return self._reject(filename, "zip", digest, "encrypted archives are unsupported")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        return self._reject(filename, "zip", digest, "symlink entries are unsafe")
                files = {
                    self._normalize_path(info.filename): archive.read(info)
                    for info in infos
                    if not info.is_dir()
                }
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            return self._reject(filename, "zip", digest, f"malformed archive: {type(exc).__name__}")
        return self._build_inspection(filename, "zip", digest, files)

    def _inspect_tar(self, filename: str, payload: bytes, digest: str) -> ArchiveInspection:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                members = archive.getmembers()
                if any(member.issym() or member.islnk() for member in members):
                    return self._reject(filename, "tar", digest, "symlink or hard-link entries are unsafe")
                files_meta = [(member.name, member.size) for member in members if member.isfile()]
                reason = self._validate_common_entries(files_meta)
                if reason:
                    return self._reject(filename, "tar", digest, reason)
                files: dict[str, bytes] = {}
                for member in members:
                    if not member.isfile():
                        continue
                    stream = archive.extractfile(member)
                    if stream is None:
                        return self._reject(filename, "tar", digest, "malformed archive member")
                    files[self._normalize_path(member.name)] = stream.read(self.limits.max_file_bytes + 1)
        except (tarfile.TarError, OSError) as exc:
            return self._reject(filename, "tar", digest, f"malformed archive: {type(exc).__name__}")
        return self._build_inspection(filename, "tar", digest, files)

    def _validate_common_entries(self, entries: list[tuple[str, int]]) -> str:
        if len(entries) > self.limits.max_files:
            return "file count exceeds safety limit"
        total = 0
        seen: set[str] = set()
        for raw_path, size in entries:
            try:
                normalized = self._normalize_path(raw_path)
            except ValueError as exc:
                return str(exc)
            collision_key = normalized.casefold()
            if collision_key in seen:
                return "duplicate normalized path collision"
            seen.add(collision_key)
            if size < 0 or size > self.limits.max_file_bytes:
                return "individual file size exceeds safety limit"
            total += size
            if total > self.limits.max_total_uncompressed_bytes:
                return "total uncompressed bytes exceed safety limit"
        return ""

    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        candidate = raw_path.replace("\\", "/")
        path = PurePosixPath(candidate)
        if path.is_absolute() or not candidate or ".." in path.parts:
            raise ValueError("path traversal or absolute archive path rejected")
        clean = "/".join(part for part in path.parts if part not in {"", "."})
        if not clean:
            raise ValueError("empty archive path rejected")
        return clean

    def _build_inspection(
        self,
        filename: str,
        kind: str,
        digest: str,
        files: dict[str, bytes],
    ) -> ArchiveInspection:
        skill_paths = sorted(path for path in files if PurePosixPath(path).name.casefold() == "skill.md")
        if not skill_paths:
            return self._reject(filename, kind, digest, "SKILL.md frontmatter not found")
        skills: list[SkillDefinition] = []
        for skill_path in skill_paths:
            try:
                text = files[skill_path].decode("utf-8")
                name, description, instructions = self._parse_skill(text)
            except (UnicodeDecodeError, ValueError) as exc:
                return self._reject(filename, kind, digest, f"invalid SKILL.md frontmatter: {exc}")
            parent = str(PurePosixPath(skill_path).parent)
            prefix = "" if parent == "." else parent + "/"
            resources = tuple(
                sorted(
                    path
                    for path in files
                    if path != skill_path and (not prefix or path.startswith(prefix))
                )
            )
            skill_id = self._skill_id(name, skill_path)
            skills.append(
                SkillDefinition(
                    skill_id=skill_id,
                    name=name,
                    description=description,
                    instructions=instructions,
                    source_path=skill_path,
                    resources=resources,
                )
            )
        return ArchiveInspection(filename, kind, digest, tuple(skills))

    @staticmethod
    def _parse_skill(text: str) -> tuple[str, str, str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("frontmatter opening delimiter is required")
        try:
            closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration as exc:
            raise ValueError("frontmatter closing delimiter is required") from exc
        values: dict[str, str] = {}
        for line in lines[1:closing]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition(":")
            if not separator:
                raise ValueError("frontmatter entries must use key: value")
            values[key.strip().casefold()] = value.strip().strip('"\'')
        name = values.get("name", "").strip()
        description = values.get("description", "").strip()
        if not name or not description:
            raise ValueError("frontmatter requires name and description")
        return name, description, "\n".join(lines[closing + 1 :]).strip()

    @staticmethod
    def _skill_id(name: str, source_path: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if slug:
            return slug
        return "skill-" + hashlib.sha256(f"{name}\n{source_path}".encode()).hexdigest()[:12]

    @staticmethod
    def _reject(filename: str, kind: str, digest: str, reason: str) -> ArchiveInspection:
        return ArchiveInspection(filename, kind, digest, (), rejected=True, reason=reason)
