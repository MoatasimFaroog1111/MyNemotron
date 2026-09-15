from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nemotron.staff.domain.skills import SkillDefinition


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    filename: str
    archive_kind: str
    package_sha256: str
    skills: tuple[SkillDefinition, ...]
    rejected: bool = False
    reason: str = ""


class ArchiveInspector(Protocol):
    def inspect(self, filename: str, payload: bytes) -> ArchiveInspection: ...
