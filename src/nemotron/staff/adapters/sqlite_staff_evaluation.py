from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationRunMode,
    EvaluationSuiteIdentity,
    OfficeEvaluationReport,
    ReadinessFailure,
    ReadinessResult,
    StaffCaseScore,
    StaffEvaluationReport,
)


class SQLiteStaffEvaluationReportRepository:
    """Append-only SQLite storage for immutable staff and office evaluation reports."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS staff_evaluation_reports (
                    report_id TEXT PRIMARY KEY,
                    office_run_id TEXT,
                    staff_id TEXT NOT NULL,
                    suite_id TEXT NOT NULL,
                    dataset_digest TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_staff_evaluation_staff_time
                    ON staff_evaluation_reports(staff_id, measured_at DESC, report_id DESC);
                CREATE INDEX IF NOT EXISTS idx_staff_evaluation_office_run
                    ON staff_evaluation_reports(office_run_id, staff_id);

                CREATE TABLE IF NOT EXISTS office_evaluation_reports (
                    run_id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    dataset_digest TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def append_staff(self, report: StaffEvaluationReport) -> None:
        payload = _json_dump(_staff_report_to_payload(report))
        self._append(
            """INSERT INTO staff_evaluation_reports (
                report_id, office_run_id, staff_id, suite_id, dataset_digest,
                mode, measured_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report.report_id,
                report.office_run_id,
                report.staff_id,
                report.suite.suite_id,
                report.suite.dataset_digest,
                report.mode.value,
                _ordered_timestamp(report.measured_at),
                payload,
            ),
        )

    def get_staff(self, report_id: str) -> StaffEvaluationReport | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM staff_evaluation_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return _staff_report_from_payload(json.loads(row["payload_json"]))

    def list_staff(
        self,
        staff_id: str | None = None,
        *,
        limit: int = 100,
    ) -> tuple[StaffEvaluationReport, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as db:
            if staff_id is None:
                rows = db.execute(
                    """SELECT payload_json FROM staff_evaluation_reports
                    ORDER BY measured_at DESC, report_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT payload_json FROM staff_evaluation_reports
                    WHERE staff_id = ?
                    ORDER BY measured_at DESC, report_id DESC LIMIT ?""",
                    (staff_id, limit),
                ).fetchall()
        return tuple(_staff_report_from_payload(json.loads(row["payload_json"])) for row in rows)

    def append_office(self, report: OfficeEvaluationReport) -> None:
        payload = _json_dump(_office_report_to_payload(report))
        self._append(
            """INSERT INTO office_evaluation_reports (
                run_id, suite_id, dataset_digest, mode, measured_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                report.run_id,
                report.suite_id,
                report.dataset_digest,
                report.mode.value,
                _ordered_timestamp(report.measured_at),
                payload,
            ),
        )

    def get_office(self, run_id: str) -> OfficeEvaluationReport | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM office_evaluation_reports WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _office_report_from_payload(json.loads(row["payload_json"]))

    def list_completed_staff(self, run_id: str) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT DISTINCT staff_id FROM staff_evaluation_reports
                WHERE office_run_id = ? ORDER BY staff_id""",
                (run_id,),
            ).fetchall()
        return tuple(row["staff_id"] for row in rows)

    def _append(self, sql: str, params: tuple[object, ...]) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(sql, params)
            db.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise ValueError("evaluation report already exists") from exc
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()


def _ordered_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _score_to_payload(score: StaffCaseScore) -> dict[str, Any]:
    return {
        "case_id": score.case_id,
        "category": score.category.value,
        "passed": score.passed,
        "correctness": score.correctness,
        "safety": score.safety,
        "recovery": score.recovery,
        "language_compliance": score.language_compliance,
        "latency_ms": score.latency_ms,
        "cost_usd": score.cost_usd,
        "failure_reasons": list(score.failure_reasons),
        "blocked": score.blocked,
        "attempts": score.attempts,
        "recovered_after_failure": score.recovered_after_failure,
        "provider_latency_ms": score.provider_latency_ms,
        "prompt_tokens": score.prompt_tokens,
        "completion_tokens": score.completion_tokens,
        "total_tokens": score.total_tokens,
    }


def _score_from_payload(payload: dict[str, Any]) -> StaffCaseScore:
    return StaffCaseScore(
        case_id=str(payload["case_id"]),
        category=EvaluationCategory(str(payload["category"])),
        passed=bool(payload["passed"]),
        correctness=payload.get("correctness"),
        safety=payload.get("safety"),
        recovery=payload.get("recovery"),
        language_compliance=payload.get("language_compliance"),
        latency_ms=float(payload["latency_ms"]),
        cost_usd=payload.get("cost_usd"),
        failure_reasons=tuple(str(value) for value in payload.get("failure_reasons", [])),
        blocked=bool(payload.get("blocked", False)),
        attempts=int(payload.get("attempts", 1)),
        recovered_after_failure=bool(payload.get("recovered_after_failure", False)),
        provider_latency_ms=payload.get("provider_latency_ms"),
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        total_tokens=payload.get("total_tokens"),
    )


def _readiness_to_payload(readiness: ReadinessResult) -> dict[str, Any]:
    return {
        "ready": readiness.ready,
        "status": readiness.status,
        "failures": [failure.value for failure in readiness.failures],
    }


def _readiness_from_payload(payload: dict[str, Any]) -> ReadinessResult:
    return ReadinessResult(
        ready=bool(payload["ready"]),
        status=str(payload["status"]),
        failures=tuple(ReadinessFailure(str(value)) for value in payload.get("failures", [])),
    )


def _staff_report_to_payload(report: StaffEvaluationReport) -> dict[str, Any]:
    return {
        "report_id": report.report_id,
        "office_run_id": report.office_run_id,
        "staff_id": report.staff_id,
        "suite": {
            "suite_id": report.suite.suite_id,
            "dataset_digest": report.suite.dataset_digest,
        },
        "mode": report.mode.value,
        "model_id": report.model_id,
        "config_digest": report.config_digest,
        "git_sha": report.git_sha,
        "scores": [_score_to_payload(score) for score in report.scores],
        "sample_size": report.sample_size,
        "correctness_rate": report.correctness_rate,
        "safety_pass_rate": report.safety_pass_rate,
        "recovery_rate": report.recovery_rate,
        "blocked_rate": report.blocked_rate,
        "average_latency_ms": report.average_latency_ms,
        "p50_latency_ms": report.p50_latency_ms,
        "p95_latency_ms": report.p95_latency_ms,
        "average_attempts": report.average_attempts,
        "prompt_tokens": report.prompt_tokens,
        "completion_tokens": report.completion_tokens,
        "total_tokens": report.total_tokens,
        "usage_measured_cases": report.usage_measured_cases,
        "average_cost_usd": report.average_cost_usd,
        "cost_measured_cases": report.cost_measured_cases,
        "failed_case_ids": list(report.failed_case_ids),
        "governance_violations": list(report.governance_violations),
        "readiness": _readiness_to_payload(report.readiness),
        "measured_at": report.measured_at.isoformat(),
    }


def _staff_report_from_payload(payload: dict[str, Any]) -> StaffEvaluationReport:
    suite = payload["suite"]
    return StaffEvaluationReport(
        report_id=str(payload["report_id"]),
        office_run_id=payload.get("office_run_id"),
        staff_id=str(payload["staff_id"]),
        suite=EvaluationSuiteIdentity(
            suite_id=str(suite["suite_id"]),
            dataset_digest=str(suite["dataset_digest"]),
        ),
        mode=EvaluationRunMode(str(payload["mode"])),
        model_id=str(payload["model_id"]),
        config_digest=str(payload["config_digest"]),
        git_sha=payload.get("git_sha"),
        scores=tuple(_score_from_payload(item) for item in payload["scores"]),
        sample_size=int(payload["sample_size"]),
        correctness_rate=payload.get("correctness_rate"),
        safety_pass_rate=payload.get("safety_pass_rate"),
        recovery_rate=payload.get("recovery_rate"),
        blocked_rate=float(payload["blocked_rate"]),
        average_latency_ms=float(payload["average_latency_ms"]),
        p50_latency_ms=float(payload["p50_latency_ms"]),
        p95_latency_ms=float(payload["p95_latency_ms"]),
        average_attempts=float(payload["average_attempts"]),
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        total_tokens=payload.get("total_tokens"),
        usage_measured_cases=int(payload["usage_measured_cases"]),
        average_cost_usd=payload.get("average_cost_usd"),
        cost_measured_cases=int(payload["cost_measured_cases"]),
        failed_case_ids=tuple(str(value) for value in payload.get("failed_case_ids", [])),
        governance_violations=tuple(
            str(value) for value in payload.get("governance_violations", [])
        ),
        readiness=_readiness_from_payload(payload["readiness"]),
        measured_at=datetime.fromisoformat(str(payload["measured_at"])),
    )


def _office_report_to_payload(report: OfficeEvaluationReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "suite_id": report.suite_id,
        "dataset_digest": report.dataset_digest,
        "mode": report.mode.value,
        "reports": [_staff_report_to_payload(item) for item in report.reports],
        "measured_at": report.measured_at.isoformat(),
    }


def _office_report_from_payload(payload: dict[str, Any]) -> OfficeEvaluationReport:
    return OfficeEvaluationReport(
        run_id=str(payload["run_id"]),
        suite_id=str(payload["suite_id"]),
        dataset_digest=str(payload["dataset_digest"]),
        mode=EvaluationRunMode(str(payload["mode"])),
        reports=tuple(_staff_report_from_payload(item) for item in payload["reports"]),
        measured_at=datetime.fromisoformat(str(payload["measured_at"])),
    )
