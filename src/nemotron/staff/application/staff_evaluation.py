from __future__ import annotations

import hashlib
from dataclasses import dataclass
from statistics import fmean

from nemotron.staff.application.evaluation_ports import (
    EvaluationClock,
    StaffEvaluationCaseRepository,
    StaffEvaluationReportRepository,
    StaffEvaluationRunner,
)
from nemotron.staff.application.evaluation_scoring import score_case
from nemotron.staff.application.ports import AuditPort, GovernanceAuditEvent
from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationComparison,
    EvaluationRunMode,
    OfficeEvaluationReport,
    StaffCaseScore,
    StaffEvaluationReport,
    StaffReadinessPolicy,
    nearest_rank_percentile,
)


@dataclass(frozen=True, slots=True)
class RunStaffEvaluationRequest:
    staff_id: str
    suite_id: str
    mode: EvaluationRunMode
    report_id: str
    model_id: str
    config_digest: str
    git_sha: str | None

    def __post_init__(self) -> None:
        for name, value in {
            "staff_id": self.staff_id,
            "suite_id": self.suite_id,
            "report_id": self.report_id,
            "model_id": self.model_id,
            "config_digest": self.config_digest,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.git_sha is not None and not self.git_sha.strip():
            raise ValueError("git_sha cannot be blank")


class RunStaffEvaluation:
    """Run one immutable staff evaluation suite and persist only a complete report."""

    def __init__(
        self,
        *,
        cases: StaffEvaluationCaseRepository,
        runner: StaffEvaluationRunner,
        reports: StaffEvaluationReportRepository,
        clock: EvaluationClock,
        audit: AuditPort,
        policy: StaffReadinessPolicy | None = None,
        request: RunStaffEvaluationRequest | None = None,
    ) -> None:
        self._cases = cases
        self._runner = runner
        self._reports = reports
        self._clock = clock
        self._audit = audit
        self._policy = policy or StaffReadinessPolicy()
        self._request = request

    def __call__(
        self,
        request: RunStaffEvaluationRequest | None = None,
        *,
        office_run_id: str | None = None,
    ) -> StaffEvaluationReport:
        selected = request or self._request
        if selected is None:
            raise ValueError("RunStaffEvaluationRequest is required")
        if office_run_id is not None and not office_run_id.strip():
            raise ValueError("office_run_id cannot be blank")

        suite, cases = self._cases.load_suite(selected.staff_id, selected.suite_id)
        if suite.suite_id != selected.suite_id:
            raise ValueError("evaluation repository returned an unexpected suite id")
        if not cases:
            raise ValueError("staff evaluation suite cannot be empty")
        if any(case.staff_id != selected.staff_id for case in cases):
            raise ValueError("evaluation suite contains a case assigned to another staff member")
        case_ids = tuple(case.case_id for case in cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation suite contains duplicate case ids")

        scores: list[StaffCaseScore] = []
        for case in cases:
            outcome = self._runner.run(case)
            score = score_case(case, outcome)
            scores.append(score)
            self._audit.append(
                GovernanceAuditEvent(
                    "evaluation.case_completed",
                    "evaluation_case",
                    case.case_id,
                    "staff-evaluation",
                    self._clock.now(),
                    (
                        f"staff_id={selected.staff_id}; category={case.category.value}; "
                        f"status={'passed' if score.passed else 'failed'}; "
                        f"duration_ms={score.latency_ms:.3f}"
                    ),
                )
            )

        score_tuple = tuple(scores)
        correctness_scores = tuple(
            score.correctness
            for score in score_tuple
            if score.category is EvaluationCategory.CORRECTNESS and score.correctness is not None
        )
        safety_scores = tuple(
            score.safety
            for score in score_tuple
            if score.category is EvaluationCategory.SAFETY and score.safety is not None
        )
        recovery_scores = tuple(
            score.recovery
            for score in score_tuple
            if score.category is EvaluationCategory.RECOVERY and score.recovery is not None
        )
        latencies = tuple(score.latency_ms for score in score_tuple)

        correctness_rate = fmean(correctness_scores) if correctness_scores else None
        safety_pass_rate = (
            sum(1 for value in safety_scores if value == 1.0) / len(safety_scores)
            if safety_scores
            else None
        )
        recovery_rate = (
            sum(1 for value in recovery_scores if value == 1.0) / len(recovery_scores)
            if recovery_scores
            else None
        )
        blocked_rate = sum(1 for score in score_tuple if score.blocked) / len(score_tuple)
        average_latency_ms = fmean(latencies)
        p50_latency_ms = nearest_rank_percentile(latencies, 0.50)
        p95_latency_ms = nearest_rank_percentile(latencies, 0.95)
        average_attempts = fmean(tuple(float(score.attempts) for score in score_tuple))

        usage_scores = tuple(
            score
            for score in score_tuple
            if score.prompt_tokens is not None
            or score.completion_tokens is not None
            or score.total_tokens is not None
        )
        prompt_tokens = _sum_optional(score.prompt_tokens for score in score_tuple)
        completion_tokens = _sum_optional(score.completion_tokens for score in score_tuple)
        total_tokens = _sum_optional(score.total_tokens for score in score_tuple)
        cost_values = tuple(score.cost_usd for score in score_tuple if score.cost_usd is not None)
        average_cost_usd = fmean(cost_values) if cost_values else None

        failed_case_ids = tuple(score.case_id for score in score_tuple if not score.passed)
        governance_violations = tuple(
            f"{score.case_id}:unexpected_execution_reference"
            for score in score_tuple
            if "unexpected_execution_reference" in score.failure_reasons
        )
        readiness = self._policy.evaluate(
            mode=selected.mode,
            sample_size=len(score_tuple),
            correctness_rate=correctness_rate,
            safety_pass_rate=safety_pass_rate,
            recovery_rate=recovery_rate,
            p95_latency_ms=p95_latency_ms,
            governance_violations=governance_violations,
        )
        measured_at = self._clock.now()
        report = StaffEvaluationReport(
            report_id=selected.report_id,
            office_run_id=office_run_id,
            staff_id=selected.staff_id,
            suite=suite,
            mode=selected.mode,
            model_id=selected.model_id,
            config_digest=selected.config_digest,
            git_sha=selected.git_sha,
            scores=score_tuple,
            sample_size=len(score_tuple),
            correctness_rate=correctness_rate,
            safety_pass_rate=safety_pass_rate,
            recovery_rate=recovery_rate,
            blocked_rate=blocked_rate,
            average_latency_ms=average_latency_ms,
            p50_latency_ms=p50_latency_ms,
            p95_latency_ms=p95_latency_ms,
            average_attempts=average_attempts,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_measured_cases=len(usage_scores),
            average_cost_usd=average_cost_usd,
            cost_measured_cases=len(cost_values),
            failed_case_ids=failed_case_ids,
            governance_violations=governance_violations,
            readiness=readiness,
            measured_at=measured_at,
        )

        # Persistence deliberately occurs only after every case has run, scored,
        # aggregated, and passed domain construction validation.
        self._reports.append_staff(report)
        self._audit.append(
            GovernanceAuditEvent(
                "evaluation.staff_completed",
                "evaluation_report",
                report.report_id,
                "staff-evaluation",
                measured_at,
                (
                    f"staff_id={report.staff_id}; suite_id={report.suite.suite_id}; "
                    f"mode={report.mode.value}; sample_size={report.sample_size}; "
                    f"status={report.readiness.status}; p95_ms={report.p95_latency_ms:.3f}"
                ),
            )
        )
        return report


class RunOfficeEvaluation:
    """Run or resume a governed staff evaluation without averaging away failures."""

    def __init__(
        self,
        *,
        run_staff: RunStaffEvaluation,
        reports: StaffEvaluationReportRepository,
        clock: EvaluationClock,
        audit: AuditPort,
        staff_ids: tuple[str, ...],
    ) -> None:
        if not staff_ids or any(not value.strip() for value in staff_ids):
            raise ValueError("office evaluation requires non-empty staff ids")
        if len(staff_ids) != len(set(staff_ids)):
            raise ValueError("office evaluation staff ids must be unique")
        self._run_staff = run_staff
        self._reports = reports
        self._clock = clock
        self._audit = audit
        self._staff_ids = staff_ids

    def __call__(
        self,
        *,
        suite_id: str,
        mode: EvaluationRunMode,
        run_id: str,
        model_id: str,
        config_digest: str,
        git_sha: str | None,
    ) -> OfficeEvaluationReport:
        for name, value in {
            "suite_id": suite_id,
            "run_id": run_id,
            "model_id": model_id,
            "config_digest": config_digest,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} is required")

        existing_office = self._reports.get_office(run_id)
        if existing_office is not None:
            _validate_resumed_office(
                existing_office,
                staff_ids=self._staff_ids,
                suite_id=suite_id,
                mode=mode,
                model_id=model_id,
                config_digest=config_digest,
                git_sha=git_sha,
            )
            return existing_office

        completed_staff = set(self._reports.list_completed_staff(run_id))
        unexpected_staff = completed_staff.difference(self._staff_ids)
        if unexpected_staff:
            raise ValueError("office evaluation resume contains unexpected staff reports")

        staff_reports: list[StaffEvaluationReport] = []
        for staff_id in self._staff_ids:
            request = RunStaffEvaluationRequest(
                staff_id=staff_id,
                suite_id=suite_id,
                mode=mode,
                report_id=f"{run_id}:{staff_id}",
                model_id=model_id,
                config_digest=config_digest,
                git_sha=git_sha,
            )
            if staff_id in completed_staff:
                existing = self._reports.get_staff(request.report_id)
                if existing is None:
                    raise ValueError("office evaluation resume index references a missing report")
                _validate_resumed_staff(existing, request=request, office_run_id=run_id)
                staff_reports.append(existing)
                self._audit.append(
                    GovernanceAuditEvent(
                        "evaluation.staff_resumed",
                        "evaluation_report",
                        existing.report_id,
                        "staff-evaluation",
                        self._clock.now(),
                        (
                            f"staff_id={staff_id}; suite_id={suite_id}; "
                            f"mode={mode.value}; office_run_id={run_id}"
                        ),
                    )
                )
                continue
            staff_reports.append(self._run_staff(request, office_run_id=run_id))

        report_tuple = tuple(staff_reports)
        dataset_digest = _office_dataset_digest(report_tuple)
        measured_at = self._clock.now()
        office = OfficeEvaluationReport(
            run_id=run_id,
            suite_id=suite_id,
            dataset_digest=dataset_digest,
            mode=mode,
            reports=report_tuple,
            measured_at=measured_at,
        )
        self._reports.append_office(office)
        self._audit.append(
            GovernanceAuditEvent(
                "evaluation.office_completed",
                "evaluation_office_run",
                run_id,
                "staff-evaluation",
                measured_at,
                (
                    f"suite_id={suite_id}; mode={mode.value}; staff_count={len(report_tuple)}; "
                    f"status={'ready' if office.ready else 'not_ready'}"
                ),
            )
        )
        return office


class CompareEvaluationRuns:
    """Compare immutable staff reports while failing closed on incompatible datasets."""

    def __call__(
        self,
        left: StaffEvaluationReport,
        right: StaffEvaluationReport,
        *,
        allow_incompatible: bool = False,
    ) -> EvaluationComparison:
        if left.staff_id != right.staff_id:
            raise ValueError("evaluation reports for different staff are incompatible")
        compatible = (
            left.suite.suite_id == right.suite.suite_id
            and left.suite.dataset_digest == right.suite.dataset_digest
        )
        if not compatible and not allow_incompatible:
            raise ValueError("evaluation reports use incompatible dataset identities")

        return EvaluationComparison(
            staff_id=left.staff_id,
            left_report_id=left.report_id,
            right_report_id=right.report_id,
            compatible=compatible,
            correctness_delta=_optional_delta(left.correctness_rate, right.correctness_rate),
            safety_delta=_optional_delta(left.safety_pass_rate, right.safety_pass_rate),
            recovery_delta=_optional_delta(left.recovery_rate, right.recovery_rate),
            p95_latency_delta_ms=right.p95_latency_ms - left.p95_latency_ms,
            blocked_rate_delta=right.blocked_rate - left.blocked_rate,
            average_cost_delta_usd=_optional_delta(left.average_cost_usd, right.average_cost_usd),
        )


def _validate_resumed_staff(
    report: StaffEvaluationReport,
    *,
    request: RunStaffEvaluationRequest,
    office_run_id: str,
) -> None:
    compatible = (
        report.report_id == request.report_id
        and report.office_run_id == office_run_id
        and report.staff_id == request.staff_id
        and report.suite.suite_id == request.suite_id
        and report.mode is request.mode
        and report.model_id == request.model_id
        and report.config_digest == request.config_digest
        and report.git_sha == request.git_sha
    )
    if not compatible:
        raise ValueError("completed evaluation report is incompatible with requested office run")


def _validate_resumed_office(
    report: OfficeEvaluationReport,
    *,
    staff_ids: tuple[str, ...],
    suite_id: str,
    mode: EvaluationRunMode,
    model_id: str,
    config_digest: str,
    git_sha: str | None,
) -> None:
    if report.suite_id != suite_id or report.mode is not mode:
        raise ValueError("completed office evaluation is incompatible with requested run")
    if tuple(item.staff_id for item in report.reports) != staff_ids:
        raise ValueError("completed office evaluation has an incompatible staff roster")
    for item in report.reports:
        request = RunStaffEvaluationRequest(
            staff_id=item.staff_id,
            suite_id=suite_id,
            mode=mode,
            report_id=f"{report.run_id}:{item.staff_id}",
            model_id=model_id,
            config_digest=config_digest,
            git_sha=git_sha,
        )
        _validate_resumed_staff(item, request=request, office_run_id=report.run_id)


def _sum_optional(values) -> int | None:  # type: ignore[no-untyped-def]
    measured = tuple(value for value in values if value is not None)
    return sum(measured) if measured else None


def _optional_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return right - left


def _office_dataset_digest(reports: tuple[StaffEvaluationReport, ...]) -> str:
    payload = "\n".join(
        f"{report.staff_id}:{report.suite.suite_id}:{report.suite.dataset_digest}"
        for report in sorted(reports, key=lambda item: item.staff_id)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
