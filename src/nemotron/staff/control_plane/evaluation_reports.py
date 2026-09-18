from __future__ import annotations

from nemotron.staff.adapters.sqlite_staff_evaluation import SQLiteStaffEvaluationReportRepository
from nemotron.staff.domain.staff_evaluation import StaffCaseScore, StaffEvaluationReport


class EvaluationReportQueries:
    """Read-only, redacted view over immutable persisted staff evaluation reports."""

    def __init__(self, reports: SQLiteStaffEvaluationReportRepository) -> None:
        self._reports = reports

    def list_staff(self, *, limit: int = 100) -> list[dict[str, object]]:
        return [_report_summary(report) for report in self._reports.list_staff(limit=limit)]

    def latest_for_staff(self, staff_id: str) -> dict[str, object]:
        normalized = staff_id.strip()
        if not normalized:
            raise ValueError("staff_id is required")
        reports = self._reports.list_staff(normalized, limit=1)
        if not reports:
            return {"staff_id": normalized, "status": "unmeasured"}
        return _report_summary(reports[0])

    def report(self, report_id: str) -> dict[str, object]:
        normalized = report_id.strip()
        if not normalized:
            raise ValueError("report_id is required")
        report = self._reports.get_staff(normalized)
        if report is None:
            raise LookupError("evaluation report not found")
        return _full_report(report)


def _readiness(report: StaffEvaluationReport) -> dict[str, object]:
    return {
        "ready": report.readiness.ready,
        "status": report.readiness.status,
        "failures": [failure.value for failure in report.readiness.failures],
    }


def _report_summary(report: StaffEvaluationReport) -> dict[str, object]:
    return {
        "status": "measured",
        "report_id": report.report_id,
        "office_run_id": report.office_run_id,
        "staff_id": report.staff_id,
        "suite_id": report.suite.suite_id,
        "dataset_digest": report.suite.dataset_digest,
        "mode": report.mode.value,
        "model_id": report.model_id,
        "git_sha": report.git_sha,
        "sample_size": report.sample_size,
        "correctness_rate": report.correctness_rate,
        "safety_pass_rate": report.safety_pass_rate,
        "recovery_rate": report.recovery_rate,
        "blocked_rate": report.blocked_rate,
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
        "readiness": _readiness(report),
        "measured_at": report.measured_at.isoformat(),
    }


def _score(score: StaffCaseScore) -> dict[str, object]:
    return {
        "case_id": score.case_id,
        "category": score.category.value,
        "passed": score.passed,
        "correctness": score.correctness,
        "safety": score.safety,
        "recovery": score.recovery,
        "language_compliance": score.language_compliance,
        "latency_ms": score.latency_ms,
        "provider_latency_ms": score.provider_latency_ms,
        "cost_usd": score.cost_usd,
        "failure_reasons": list(score.failure_reasons),
        "blocked": score.blocked,
        "attempts": score.attempts,
        "recovered_after_failure": score.recovered_after_failure,
        "prompt_tokens": score.prompt_tokens,
        "completion_tokens": score.completion_tokens,
        "total_tokens": score.total_tokens,
    }


def _full_report(report: StaffEvaluationReport) -> dict[str, object]:
    payload = _report_summary(report)
    payload["scores"] = [_score(score) for score in report.scores]
    return payload
