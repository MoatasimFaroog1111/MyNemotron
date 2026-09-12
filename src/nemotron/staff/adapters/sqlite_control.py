from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nemotron.staff.application.ports import AuditEvent, AuditRecord, GovernanceAuditEvent
from nemotron.staff.domain import Permission, RiskLevel, Role, StaffMember, StaffStatus
from nemotron.staff.domain.organization import Department, Organization, StaffPlacement


@dataclass(frozen=True, slots=True)
class AuditRecordView:
    audit_id: int
    event_type: str
    subject_type: str
    subject_id: str
    actor_id: str
    occurred_at: datetime
    detail: str


class SQLiteControlStore:
    """Durable staff registry, organization structure, and append-only audit log."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS staff_registry (
                    staff_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staff_organizations (
                    organization_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staff_audit_log (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_staff_audit_time
                    ON staff_audit_log(occurred_at DESC, audit_id DESC);
                CREATE INDEX IF NOT EXISTS idx_staff_audit_subject
                    ON staff_audit_log(subject_type, subject_id, audit_id DESC);
                """
            )

    def check(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)


class SQLiteStaffRepository:
    def __init__(self, store: SQLiteControlStore) -> None:
        self.store = store

    @staticmethod
    def _serialize(member: StaffMember) -> str:
        payload = {
            "staff_id": member.staff_id,
            "display_name": member.display_name,
            "status": member.status.value,
            "role": {
                "role_id": member.role.role_id,
                "name": member.role.name,
                "approval_limit": member.role.approval_limit.value if member.role.approval_limit else None,
                "permissions": [
                    {
                        "action": permission.action,
                        "resource": permission.resource,
                        "max_risk": permission.max_risk.value,
                    }
                    for permission in member.role.permissions
                ],
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _deserialize(value: str) -> StaffMember:
        payload = json.loads(value)
        role_raw = payload["role"]
        approval_limit = role_raw.get("approval_limit")
        role = Role(
            role_id=role_raw["role_id"],
            name=role_raw["name"],
            permissions=tuple(
                Permission(
                    action=item["action"],
                    resource=item["resource"],
                    max_risk=RiskLevel(item["max_risk"]),
                )
                for item in role_raw.get("permissions", [])
            ),
            approval_limit=RiskLevel(approval_limit) if approval_limit else None,
        )
        return StaffMember(
            staff_id=payload["staff_id"],
            display_name=payload["display_name"],
            role=role,
            status=StaffStatus(payload["status"]),
        )

    def get(self, staff_id: str) -> StaffMember:
        with self.store._connect() as db:
            row = db.execute("SELECT payload_json FROM staff_registry WHERE staff_id = ?", (staff_id,)).fetchone()
        if row is None:
            raise LookupError(staff_id)
        return self._deserialize(row["payload_json"])

    def save(self, member: StaffMember) -> None:
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO staff_registry(staff_id, payload_json) VALUES (?, ?)
                ON CONFLICT(staff_id) DO UPDATE SET payload_json = excluded.payload_json""",
                (member.staff_id, self._serialize(member)),
            )

    def list_all(self) -> tuple[StaffMember, ...]:
        with self.store._connect() as db:
            rows = db.execute("SELECT payload_json FROM staff_registry ORDER BY staff_id").fetchall()
        return tuple(self._deserialize(row["payload_json"]) for row in rows)


class SQLiteOrganizationRepository:
    def __init__(self, store: SQLiteControlStore) -> None:
        self.store = store

    @staticmethod
    def _serialize(organization: Organization) -> str:
        payload = {
            "organization_id": organization.organization_id,
            "name": organization.name,
            "chief_of_staff_id": organization.chief_of_staff_id,
            "departments": [
                {
                    "department_id": department.department_id,
                    "name": department.name,
                    "parent_department_id": department.parent_department_id,
                }
                for department in organization.departments
            ],
            "placements": [
                {
                    "staff_id": placement.staff_id,
                    "department_id": placement.department_id,
                    "job_title": placement.job_title,
                    "manager_id": placement.manager_id,
                }
                for placement in organization.placements
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _deserialize(value: str) -> Organization:
        payload = json.loads(value)
        return Organization(
            organization_id=payload["organization_id"],
            name=payload["name"],
            departments=tuple(
                Department(
                    department_id=item["department_id"],
                    name=item["name"],
                    parent_department_id=item.get("parent_department_id"),
                )
                for item in payload.get("departments", [])
            ),
            placements=tuple(
                StaffPlacement(
                    staff_id=item["staff_id"],
                    department_id=item["department_id"],
                    job_title=item["job_title"],
                    manager_id=item.get("manager_id"),
                )
                for item in payload.get("placements", [])
            ),
            chief_of_staff_id=payload.get("chief_of_staff_id"),
        )

    def get(self, organization_id: str) -> Organization:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM staff_organizations WHERE organization_id = ?",
                (organization_id,),
            ).fetchone()
        if row is None:
            raise LookupError(organization_id)
        return self._deserialize(row["payload_json"])

    def save(self, organization: Organization) -> None:
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO staff_organizations(organization_id, payload_json) VALUES (?, ?)
                ON CONFLICT(organization_id) DO UPDATE SET payload_json = excluded.payload_json""",
                (organization.organization_id, self._serialize(organization)),
            )

    def list_all(self) -> tuple[Organization, ...]:
        with self.store._connect() as db:
            rows = db.execute("SELECT payload_json FROM staff_organizations ORDER BY organization_id").fetchall()
        return tuple(self._deserialize(row["payload_json"]) for row in rows)


class SQLiteAuditLog:
    def __init__(self, store: SQLiteControlStore) -> None:
        self.store = store

    def append(self, event: AuditRecord) -> None:
        if isinstance(event, AuditEvent):
            subject_type = "task"
            subject_id = event.task_id
        elif isinstance(event, GovernanceAuditEvent):
            subject_type = event.subject_type
            subject_id = event.subject_id
        else:
            raise TypeError(f"Unsupported audit event {type(event).__name__}.")
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO staff_audit_log(
                    event_type, subject_type, subject_id, actor_id, occurred_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_type,
                    subject_type,
                    subject_id,
                    event.actor_id,
                    event.occurred_at.isoformat(),
                    event.detail,
                ),
            )

    def list_recent(
        self,
        *,
        limit: int = 100,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> tuple[AuditRecordView, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Audit limit must be between 1 and 1000.")
        clauses: list[str] = []
        parameters: list[object] = []
        if subject_type is not None:
            clauses.append("subject_type = ?")
            parameters.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            parameters.append(subject_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT * FROM staff_audit_log"
                + where
                + " ORDER BY occurred_at DESC, audit_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(
            AuditRecordView(
                audit_id=int(row["audit_id"]),
                event_type=row["event_type"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                actor_id=row["actor_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                detail=row["detail"],
            )
            for row in rows
        )
