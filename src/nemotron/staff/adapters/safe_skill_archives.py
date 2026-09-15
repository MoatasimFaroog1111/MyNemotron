from __future__ import annotations

import hashlib
import io
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from nemotron.staff.application.skill_ports import ArchiveInspection
from nemotron.staff.domain.skills import SkillDefinition


@dataclass(frozen=True, slots=True)
class ArchiveSafetyLimits:
    max_upload_bytes: int = 25 * 1024 * 1024
    max_total_uncompressed_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 2_000
    max_nested_depth: int = 2

    def __post_init__(self) -> None:
        if min(
            self.max_upload_bytes,
            self.max_total_uncompressed_bytes,
            self.max_file_bytes,
            self.max_files,
        ) < 1:
            raise ValueError("Archive safety limits must be positive.")
        if self.max_nested_depth < 0:
            raise ValueError("max_nested_depth cannot be negative.")


class RarArchiveReader(Protocol):
    def read_files(self, payload: bytes, *, limits: ArchiveSafetyLimits) -> dict[str, bytes]: ...


class RarFileArchiveReader:
    """Read RAR members through rarfile without extracting package paths to disk."""

    def read_files(self, payload: bytes, *, limits: ArchiveSafetyLimits) -> dict[str, bytes]:
        try:
            import rarfile
        except ImportError as exc:  # pragma: no cover - production packaging guarantees dependency
            raise RuntimeError("RAR backend is not installed") from exc

        try:
            with rarfile.RarFile(io.BytesIO(payload), errors="strict") as archive:
                if archive.needs_password():
                    raise ValueError("encrypted RAR archives are unsupported")
                infos = list(archive.infolist())
                regular = [item for item in infos if item.is_file()]
                if len(regular) > limits.max_files:
                    raise ValueError("file count exceeds safety limit")
                total = 0
                seen: set[str] = set()
                for item in infos:
                    filename = str(item.filename)
                    normalized = SafeSkillArchiveInspector._normalize_path(filename)
                    key = normalized.casefold()
                    if key in seen:
                        raise ValueError("duplicate normalized path collision")
                    seen.add(key)
                    if item.is_symlink() or getattr(item, "file_redir", None) is not None:
                        raise ValueError("RAR links and redirected entries are unsafe")
                    if item.is_dir():
                        continue
                    if not item.is_file():
                        raise ValueError("unsupported RAR member type")
                    size = int(item.file_size)
                    if size < 0 or size > limits.max_file_bytes:
                        raise ValueError("individual file size exceeds safety limit")
                    total += size
                    if total > limits.max_total_uncompressed_bytes:
                        raise ValueError("total uncompressed bytes exceed safety limit")
                    if item.needs_password():
                        raise ValueError("encrypted RAR archives are unsupported")

                files: dict[str, bytes] = {}
                for item in regular:
                    normalized = SafeSkillArchiveInspector._normalize_path(str(item.filename))
                    with archive.open(item) as stream:
                        data = stream.read(limits.max_file_bytes + 1)
                    if len(data) > limits.max_file_bytes:
                        raise ValueError("individual file size exceeds safety limit")
                    files[normalized] = data
                return files
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"malformed or unsupported RAR archive: {type(exc).__name__}") from exc


@dataclass(slots=True)
class _ArchiveBudget:
    files: int = 0
    uncompressed_bytes: int = 0


class SafeSkillArchiveInspector:
    """Inspect hostile skill archives as inert data; never execute package content."""

    _RAR_FAMILY_MAGIC = b"Rar!\x1a\x07"
    _SEVEN_Z_MAGIC = b"7z\xbc\xaf'\x1c"

    def __init__(
        self,
        *,
        limits: ArchiveSafetyLimits | None = None,
        rar_reader: RarArchiveReader | None = None,
    ) -> None:
        self.limits = limits or ArchiveSafetyLimits()
        self._rar_reader = rar_reader or RarFileArchiveReader()

    def inspect(self, filename: str, payload: bytes) -> ArchiveInspection:
        digest = hashlib.sha256(payload).hexdigest()
        kind = self._kind(payload)
        if len(payload) > self.limits.max_upload_bytes:
            return self._reject(filename, kind, digest, "compressed upload size exceeds safety limit")
        if kind == "unknown":
            return self._reject(filename, kind, digest, "unsupported archive format")
        if kind == "7z":
            return self._reject(filename, kind, digest, "unsupported 7z backend; archive was not executed")

        try:
            files: dict[str, bytes] = {}
            budget = _ArchiveBudget()
            self._collect_archive(
                payload,
                kind=kind,
                depth=0,
                prefix="",
                destination=files,
                budget=budget,
            )
            return self._build(filename, kind, digest, files)
        except (ValueError, RuntimeError, OSError) as exc:
            return self._reject(filename, kind, digest, str(exc) or type(exc).__name__)

    def _kind(self, payload: bytes) -> str:
        if payload.startswith(self._RAR_FAMILY_MAGIC):
            return "rar"
        if payload.startswith(self._SEVEN_Z_MAGIC):
            return "7z"
        if zipfile.is_zipfile(io.BytesIO(payload)):
            return "zip"
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
                return "tar"
        except (tarfile.TarError, OSError):
            return "unknown"

    def _collect_archive(
        self,
        payload: bytes,
        *,
        kind: str,
        depth: int,
        prefix: str,
        destination: dict[str, bytes],
        budget: _ArchiveBudget,
    ) -> None:
        if kind == "zip":
            members = self._read_zip_files(payload)
        elif kind == "tar":
            members = self._read_tar_files(payload)
        elif kind == "rar":
            members = self._rar_reader.read_files(payload, limits=self.limits)
            self._validate_entries([(path, len(data)) for path, data in members.items()])
            members = {self._normalize_path(path): data for path, data in members.items()}
        elif kind == "7z":
            raise ValueError("unsupported 7z backend; archive was not executed")
        else:
            raise ValueError("unsupported archive format")

        for relative_path, data in members.items():
            self._consume_budget(budget, len(data))
            virtual_path = f"{prefix}{relative_path}"
            nested_kind = self._kind(data)
            if nested_kind == "unknown":
                key = virtual_path.casefold()
                if any(existing.casefold() == key for existing in destination):
                    raise ValueError("duplicate normalized path collision")
                destination[virtual_path] = data
                continue
            if depth >= self.limits.max_nested_depth:
                raise ValueError("nested archive depth exceeds safety limit")
            self._collect_archive(
                data,
                kind=nested_kind,
                depth=depth + 1,
                prefix=f"{virtual_path}!/",
                destination=destination,
                budget=budget,
            )

    def _consume_budget(self, budget: _ArchiveBudget, size: int) -> None:
        budget.files += 1
        budget.uncompressed_bytes += size
        if budget.files > self.limits.max_files:
            raise ValueError("file count exceeds safety limit")
        if size > self.limits.max_file_bytes:
            raise ValueError("individual file size exceeds safety limit")
        if budget.uncompressed_bytes > self.limits.max_total_uncompressed_bytes:
            raise ValueError("total uncompressed bytes exceed safety limit")

    def _read_zip_files(self, payload: bytes) -> dict[str, bytes]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = archive.infolist()
                self._validate_entries([(item.filename, item.file_size) for item in infos if not item.is_dir()])
                files: dict[str, bytes] = {}
                for item in infos:
                    if item.flag_bits & 0x1:
                        raise ValueError("encrypted archives are unsupported")
                    if stat.S_ISLNK((item.external_attr >> 16) & 0xFFFF):
                        raise ValueError("symlink entries are unsafe")
                    if item.is_dir():
                        continue
                    path = self._normalize_path(item.filename)
                    data = archive.read(item)
                    if len(data) > self.limits.max_file_bytes:
                        raise ValueError("individual file size exceeds safety limit")
                    files[path] = data
                return files
        except ValueError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise ValueError(f"malformed ZIP archive: {type(exc).__name__}") from exc

    def _read_tar_files(self, payload: bytes) -> dict[str, bytes]:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                members = archive.getmembers()
                if any(item.issym() or item.islnk() for item in members):
                    raise ValueError("symlink or hard-link entries are unsafe")
                regular = [item for item in members if item.isfile()]
                self._validate_entries([(item.name, item.size) for item in regular])
                files: dict[str, bytes] = {}
                for item in regular:
                    stream = archive.extractfile(item)
                    if stream is None:
                        raise ValueError("malformed archive member")
                    data = stream.read(self.limits.max_file_bytes + 1)
                    if len(data) > self.limits.max_file_bytes:
                        raise ValueError("individual file size exceeds safety limit")
                    files[self._normalize_path(item.name)] = data
                return files
        except ValueError:
            raise
        except (tarfile.TarError, OSError) as exc:
            raise ValueError(f"malformed TAR archive: {type(exc).__name__}") from exc

    def _validate_entries(self, entries: list[tuple[str, int]]) -> None:
        if len(entries) > self.limits.max_files:
            raise ValueError("file count exceeds safety limit")
        seen: set[str] = set()
        total = 0
        for raw_path, size in entries:
            normalized = self._normalize_path(raw_path)
            key = normalized.casefold()
            if key in seen:
                raise ValueError("duplicate normalized path collision")
            seen.add(key)
            if size < 0 or size > self.limits.max_file_bytes:
                raise ValueError("individual file size exceeds safety limit")
            total += size
            if total > self.limits.max_total_uncompressed_bytes:
                raise ValueError("total uncompressed bytes exceed safety limit")

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

    def _build(self, filename: str, kind: str, digest: str, files: dict[str, bytes]) -> ArchiveInspection:
        skill_paths = sorted(path for path in files if PurePosixPath(path).name.casefold() == "skill.md")
        if not skill_paths:
            return self._reject(filename, kind, digest, "SKILL.md frontmatter not found")
        skills: list[SkillDefinition] = []
        for path in skill_paths:
            try:
                name, description, instructions = self._parse_skill(files[path].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                return self._reject(filename, kind, digest, f"invalid SKILL.md frontmatter: {exc}")
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            prefix = parent + "/" if parent else ""
            resources = tuple(sorted(item for item in files if item != path and (not prefix or item.startswith(prefix))))
            skills.append(
                SkillDefinition(
                    skill_id=self._skill_id(name, path),
                    name=name,
                    description=description,
                    instructions=instructions,
                    source_path=path,
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
        return slug or "skill-" + hashlib.sha256(f"{name}\n{source_path}".encode()).hexdigest()[:12]

    @staticmethod
    def _reject(filename: str, kind: str, digest: str, reason: str) -> ArchiveInspection:
        return ArchiveInspection(filename, kind, digest, (), rejected=True, reason=reason)
