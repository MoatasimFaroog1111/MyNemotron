from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from nemotron.staff.domain.skills import SkillDefinition, SkillVersion, TrainingAssignment, TrainingStatus


class SQLiteSkillRegistry:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _prepare(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_versions (
                    version_id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    source_filename TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    imported_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    supersedes_version_id TEXT
                );
                CREATE TABLE IF NOT EXISTS skill_assignments (
                    assignment_id TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL REFERENCES skill_versions(version_id),
                    staff_id TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    assigned_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(version_id, staff_id)
                );
                CREATE INDEX IF NOT EXISTS idx_skill_assignments_staff
                    ON skill_assignments(staff_id, status);
                """
            )

    @staticmethod
    def _content_digest(skill: SkillDefinition) -> str:
        canonical = json.dumps(
            {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "instructions": skill.instructions,
                "source_path": skill.source_path,
                "resources": list(skill.resources),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def import_version(
        self,
        *,
        package_sha256: str,
        source_filename: str,
        skill: SkillDefinition,
        imported_at: datetime,
        imported_by: str,
    ) -> SkillVersion:
        content_sha256 = self._content_digest(skill)
        version_id = f"skillv-{content_sha256[:24]}"
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO skill_versions(
                    version_id, skill_id, name, description, instructions, source_path,
                    resources_json, package_sha256, content_sha256, source_filename,
                    imported_at, imported_by, status, supersedes_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    version_id,
                    skill.skill_id,
                    skill.name,
                    skill.description,
                    skill.instructions,
                    skill.source_path,
                    json.dumps(skill.resources, ensure_ascii=False),
                    package_sha256,
                    content_sha256,
                    source_filename,
                    imported_at.isoformat(),
                    imported_by,
                    TrainingStatus.PENDING.value,
                ),
            )
        return self.get_version(version_id)

    def get_version(self, version_id: str) -> SkillVersion:
        with self._connect() as db:
            row = db.execute("SELECT * FROM skill_versions WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            raise LookupError(version_id)
        return self._version_from_row(row)

    def list_versions(self) -> tuple[SkillVersion, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM skill_versions ORDER BY imported_at, version_id").fetchall()
        return tuple(self._version_from_row(row) for row in rows)

    def set_version_status(self, version_id: str, status: TrainingStatus) -> SkillVersion:
        with self._connect() as db:
            cursor = db.execute("UPDATE skill_versions SET status = ? WHERE version_id = ?", (status.value, version_id))
            if cursor.rowcount != 1:
                raise LookupError(version_id)
        return self.get_version(version_id)

    def activate_assignments(
        self,
        version_id: str,
        staff_ids: tuple[str, ...],
        *,
        assigned_by: str,
        assigned_at: datetime,
    ) -> tuple[TrainingAssignment, ...]:
        targets = tuple(sorted(set(staff_ids)))
        if not targets:
            raise ValueError("At least one training target is required.")
        with self._connect() as db:
            row = db.execute("SELECT version_id FROM skill_versions WHERE version_id = ?", (version_id,)).fetchone()
            if row is None:
                raise LookupError(version_id)
            for staff_id in targets:
                assignment_id = "skilla-" + hashlib.sha256(f"{version_id}\n{staff_id}".encode()).hexdigest()[:24]
                db.execute(
                    """
                    INSERT OR IGNORE INTO skill_assignments(
                        assignment_id, version_id, staff_id, assigned_at, assigned_by, status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id,
                        version_id,
                        staff_id,
                        assigned_at.isoformat(),
                        assigned_by,
                        TrainingStatus.ACTIVE.value,
                    ),
                )
            db.execute(
                "UPDATE skill_versions SET status = ? WHERE version_id = ?",
                (TrainingStatus.ACTIVE.value, version_id),
            )
            placeholders = ",".join("?" for _ in targets)
            rows = db.execute(
                f"SELECT * FROM skill_assignments WHERE version_id = ? AND staff_id IN ({placeholders}) ORDER BY staff_id",
                (version_id, *targets),
            ).fetchall()
        return tuple(self._assignment_from_row(row) for row in rows)

    def resolve_staff(self, staff_id: str) -> tuple[SkillVersion, ...]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT v.* FROM skill_versions v
                JOIN skill_assignments a ON a.version_id = v.version_id
                WHERE a.staff_id = ? AND a.status = ? AND v.status = ?
                ORDER BY v.skill_id, v.version_id
                """,
                (staff_id, TrainingStatus.ACTIVE.value, TrainingStatus.ACTIVE.value),
            ).fetchall()
        return tuple(self._version_from_row(row) for row in rows)

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> SkillVersion:
        skill = SkillDefinition(
            skill_id=row["skill_id"],
            name=row["name"],
            description=row["description"],
            instructions=row["instructions"],
            source_path=row["source_path"],
            resources=tuple(json.loads(row["resources_json"])),
        )
        return SkillVersion(
            version_id=row["version_id"],
            skill=skill,
            package_sha256=row["package_sha256"],
            content_sha256=row["content_sha256"],
            imported_at=datetime.fromisoformat(row["imported_at"]),
            imported_by=row["imported_by"],
            status=TrainingStatus(row["status"]),
            supersedes_version_id=row["supersedes_version_id"],
        )

    @staticmethod
    def _assignment_from_row(row: sqlite3.Row) -> TrainingAssignment:
        return TrainingAssignment(
            assignment_id=row["assignment_id"],
            version_id=row["version_id"],
            staff_id=row["staff_id"],
            assigned_at=datetime.fromisoformat(row["assigned_at"]),
            assigned_by=row["assigned_by"],
            status=TrainingStatus(row["status"]),
        )
