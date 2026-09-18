from __future__ import annotations

from datetime import datetime, timezone

from nemotron.staff.adapters.sqlite_staff_evaluation import SQLiteStaffEvaluationReportRepository
from nemotron.staff.application.staff_evaluation import RunOfficeEvaluation, RunStaffEvaluationRequest
from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationRunMode,
    EvaluationSuiteIdentity,
    ReadinessFailure,
    ReadinessResult,
    StaffCaseScore,
    StaffEvaluationReport,
)


NOW = datetime(2026, 9, 17, 12, 30, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeAudit:
    def __init__(self) -> None:
        self.events = []

    def append(self, event) -> None:  # type: ignore[no-untyped-def]
        self.events.append(event)


class StubRunStaff:
    def __init__(self, reports: SQLiteStaffEvaluationReportRepository) -> None:
        self.reports = reports
        self.calls: list[str] = []

    def __call__(
        self,
        request: RunStaffEvaluationRequest,
        *,
        office_run_id: str | None = None,
    ) -> StaffEvaluationReport:
        self.calls.append(request.staff_id)
        report = make_report(request, office_run_id=office_run_id)
        self.reports.append_staff(report)
        return report


def make_report(
    request: RunStaffEvaluationRequest,
    *,
    office_run_id: str | None,
) -> StaffEvaluationReport:
    score = StaffCaseScore(
        case_id=f"{request.staff_id}:correctness:resume-001",
        category=EvaluationCategory.CORRECTNESS,
        passed=True,
        correctness=1.0,
        safety=None,
        recovery=None,
        language_compliance=None,
        latency_ms=100.0,
        cost_usd=None,
        failure_reasons=(),
    )
    return StaffEvaluationReport(
        report_id=request.report_id,
        office_run_id=office_run_id,
        staff_id=request.staff_id,
        suite=EvaluationSuiteIdentity(request.suite_id, "c" * 64),
        mode=request.mode,
        model_id=request.model_id,
        config_digest=request.config_digest,
        git_sha=request.git_sha,
        scores=(score,),
        sample_size=1,
        correctness_rate=1.0,
        safety_pass_rate=None,
        recovery_rate=None,
        blocked_rate=0.0,
        average_latency_ms=100.0,
        p50_latency_ms=100.0,
        p95_latency_ms=100.0,
        average_attempts=1.0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        usage_measured_cases=0,
        average_cost_usd=None,
        cost_measured_cases=0,
        failed_case_ids=(),
        governance_violations=(),
        readiness=ReadinessResult(
            False,
            ReadinessFailure.INSUFFICIENT_EVIDENCE.value,
            (ReadinessFailure.INSUFFICIENT_EVIDENCE,),
        ),
        measured_at=NOW,
    )


def test_office_evaluation_resumes_completed_staff_without_rerunning(tmp_path) -> None:
    reports = SQLiteStaffEvaluationReportRepository(tmp_path / "evaluation.db")
    run_id = "office-resume-1"
    first = "staff-data-analyst"
    second = "staff-financial-accountant"
    common = dict(
        suite_id="gold-v1",
        mode=EvaluationRunMode.LIVE,
        model_id="nemotron-test",
        config_digest="cfg-1",
        git_sha="abc123",
    )
    first_request = RunStaffEvaluationRequest(
        staff_id=first,
        report_id=f"{run_id}:{first}",
        **common,
    )
    completed = make_report(first_request, office_run_id=run_id)
    reports.append_staff(completed)

    run_staff = StubRunStaff(reports)
    office = RunOfficeEvaluation(
        run_staff=run_staff,  # type: ignore[arg-type]
        reports=reports,
        clock=FixedClock(),
        audit=FakeAudit(),
        staff_ids=(first, second),
    )
    result = office(run_id=run_id, **common)

    assert run_staff.calls == [second]
    assert result.reports[0] == completed
    assert tuple(report.staff_id for report in result.reports) == (first, second)
    assert reports.list_completed_staff(run_id) == (first, second)
    assert reports.get_office(run_id) == result
