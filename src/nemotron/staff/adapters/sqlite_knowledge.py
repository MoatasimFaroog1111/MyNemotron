from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from nemotron.staff.domain.knowledge import (
    CorrectionStatus,
    KnowledgeCorrection,
    KnowledgeError,
    KnowledgeRecord,
    KnowledgeScope,
    KnowledgeSourceKind,
)


class SQLiteKnowledgeRepository:
    """Durable provenance-aware institutional memory with atomic corrections."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS institutional_knowledge (
                    knowledge_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    owner_staff_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    source_observed_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    department_id TEXT,
                    superseded_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_institutional_knowledge_org
                    ON institutional_knowledge(organization_id, recorded_at, knowledge_id);

                CREATE TABLE IF NOT EXISTS institutional_corrections (
                    correction_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    target_knowledge_id TEXT NOT NULL,
                    proposed_content TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    source_observed_at TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    proposed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decided_by TEXT,
                    decided_at TEXT,
                    rationale TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_institutional_corrections_target
                    ON institutional_corrections(target_knowledge_id, proposed_at, correction_id);
                """
            )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> KnowledgeRecord:
        return KnowledgeRecord(
            knowledge_id=row["knowledge_id"],
            organization_id=row["organization_id"],
            owner_staff_id=row["owner_staff_id"],
            scope=KnowledgeScope(row["scope"]),
            content=row["content"],
            source_kind=KnowledgeSourceKind(row["source_kind"]),
            source_reference=row["source_reference"],
            source_observed_at=datetime.fromisoformat(row["source_observed_at"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            department_id=row["department_id"],
            superseded_by=row["superseded_by"],
        )

    @staticmethod
    def _correction_from_row(row: sqlite3.Row) -> KnowledgeCorrection:
        return KnowledgeCorrection(
            correction_id=row["correction_id"],
            organization_id=row["organization_id"],
            target_knowledge_id=row["target_knowledge_id"],
            proposed_content=row["proposed_content"],
            source_kind=KnowledgeSourceKind(row["source_kind"]),
            source_reference=row["source_reference"],
            source_observed_at=datetime.fromisoformat(row["source_observed_at"]),
            proposed_by=row["proposed_by"],
            proposed_at=datetime.fromisoformat(row["proposed_at"]),
            status=CorrectionStatus(row["status"]),
            decided_by=row["decided_by"],
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            rationale=row["rationale"],
        )

    def save_record(self, record: KnowledgeRecord) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO institutional_knowledge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.knowledge_id,
                    record.organization_id,
                    record.owner_staff_id,
                    record.scope.value,
                    record.content,
                    record.source_kind.value,
                    record.source_reference,
                    record.source_observed_at.isoformat(),
                    record.recorded_at.isoformat(),
                    record.department_id,
                    record.superseded_by,
                ),
            )

    def get_record(self, knowledge_id: str) -> KnowledgeRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM institutional_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        if row is None:
            raise LookupError(knowledge_id)
        return self._record_from_row(row)

    def search_active(self, organization_id: str, query: str) -> tuple[KnowledgeRecord, ...]:
        tokens = tuple(token.casefold() for token in query.split() if token.strip())
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM institutional_knowledge
                WHERE organization_id = ? AND superseded_by IS NULL
                ORDER BY recorded_at DESC, knowledge_id""",
                (organization_id,),
            ).fetchall()
        records = tuple(self._record_from_row(row) for row in rows)
        if not tokens:
            return records

        def haystack(record: KnowledgeRecord) -> str:
            return " ".join(
                (
                    record.content,
                    record.source_reference,
                    record.source_kind.value,
                )
            ).casefold()

        return tuple(record for record in records if all(token in haystack(record) for token in tokens))

    def save_correction(self, correction: KnowledgeCorrection) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO institutional_corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(correction_id) DO UPDATE SET
                    status=excluded.status,
                    decided_by=excluded.decided_by,
                    decided_at=excluded.decided_at,
                    rationale=excluded.rationale""",
                (
                    correction.correction_id,
                    correction.organization_id,
                    correction.target_knowledge_id,
                    correction.proposed_content,
                    correction.source_kind.value,
                    correction.source_reference,
                    correction.source_observed_at.isoformat(),
                    correction.proposed_by,
                    correction.proposed_at.isoformat(),
                    correction.status.value,
                    correction.decided_by,
                    correction.decided_at.isoformat() if correction.decided_at else None,
                    correction.rationale,
                ),
            )

    def get_correction(self, correction_id: str) -> KnowledgeCorrection:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM institutional_corrections WHERE correction_id = ?",
                (correction_id,),
            ).fetchone()
        if row is None:
            raise LookupError(correction_id)
        return self._correction_from_row(row)

    def apply_approved_correction(
        self,
        correction: KnowledgeCorrection,
        *,
        target: KnowledgeRecord,
        replacement: KnowledgeRecord,
    ) -> None:
        if correction.status is not CorrectionStatus.APPROVED:
            raise KnowledgeError("Only approved corrections may be applied.")
        if correction.target_knowledge_id != target.knowledge_id:
            raise KnowledgeError("Correction target mismatch.")
        superseded = target.supersede(replacement.knowledge_id)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT superseded_by FROM institutional_knowledge WHERE knowledge_id = ?",
                (target.knowledge_id,),
            ).fetchone()
            if current is None:
                raise LookupError(target.knowledge_id)
            if current["superseded_by"] is not None:
                raise KnowledgeError("Knowledge target was already superseded.")
            db.execute(
                "UPDATE institutional_knowledge SET superseded_by = ? WHERE knowledge_id = ? AND superseded_by IS NULL",
                (superseded.superseded_by, target.knowledge_id),
            )
            db.execute(
                """INSERT INTO institutional_knowledge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    replacement.knowledge_id,
                    replacement.organization_id,
                    replacement.owner_staff_id,
                    replacement.scope.value,
                    replacement.content,
                    replacement.source_kind.value,
                    replacement.source_reference,
                    replacement.source_observed_at.isoformat(),
                    replacement.recorded_at.isoformat(),
                    replacement.department_id,
                    replacement.superseded_by,
                ),
            )
            db.execute(
                """UPDATE institutional_corrections SET
                    status = ?, decided_by = ?, decided_at = ?, rationale = ?
                    WHERE correction_id = ? AND status = ?""",
                (
                    correction.status.value,
                    correction.decided_by,
                    correction.decided_at.isoformat() if correction.decided_at else None,
                    correction.rationale,
                    correction.correction_id,
                    CorrectionStatus.PENDING.value,
                ),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()
