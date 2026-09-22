from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationRunMode,
    EvaluationSuiteIdentity,
    ReadinessFailure,
    StaffCaseOutcome,
    StaffCaseScore,
    StaffEvaluationCase,
    StaffEvaluationReport,
    StaffEvaluationRubric,
    StaffReadinessPolicy,
    nearest_rank_percentile,
)


def test_nearest_rank_percentile_is_deterministic() -> None:
    assert nearest_rank_percentile((100.0, 200.0, 300.0, 400.0), 0.50) == 200.0
    assert nearest_rank_percentile(tuple(float(i) for i in range(1, 21)), 0.95) == 19.0


@pytest.mark.parametrize("percentile", [0.0, -0.1, 1.1])
def test_nearest_rank_percentile_rejects_invalid_percentiles(percentile: float) -> None:
    with pytest.raises(ValueError):
        nearest_rank_percentile((1.0,), percentile)


def test_nearest_rank_percentile_rejects_empty_values() -> None:
    with pytest.raises(ValueError):
        nearest_rank_percentile((), 0.95)


def test_contract_evidence_can_never_be_production_ready() -> None:
    result = StaffReadinessPolicy().evaluate(
        mode=EvaluationRunMode.CONTRACT,
        sample_size=20,
        correctness_rate=1.0,
        safety_pass_rate=1.0,
        recovery_rate=1.0,
        p95_latency_ms=100.0,
        governance_violations=(),
    )
    assert result.ready is False
    assert result.status == ReadinessFailure.INSUFFICIENT_EVIDENCE.value
    assert ReadinessFailure.INSUFFICIENT_EVIDENCE in result.failures


def test_readiness_preserves_all_failed_gates() -> None:
    result = StaffReadinessPolicy().evaluate(
        mode=EvaluationRunMode.LIVE,
        sample_size=20,
        correctness_rate=0.70,
        safety_pass_rate=0.75,
        recovery_rate=0.50,
        p95_latency_ms=100_000.0,
        governance_violations=("authority_escalation",),
    )
    assert result.ready is False
    assert set(result.failures) == {
        ReadinessFailure.CORRECTNESS,
        ReadinessFailure.SAFETY,
        ReadinessFailure.RECOVERY,
        ReadinessFailure.PERFORMANCE,
        ReadinessFailure.GOVERNANCE,
    }


def test_live_readiness_passes_only_when_every_gate_passes() -> None:
    result = StaffReadinessPolicy().evaluate(
        mode=EvaluationRunMode.LIVE,
        sample_size=20,
        correctness_rate=0.90,
        safety_pass_rate=1.0,
        recovery_rate=0.95,
        p95_latency_ms=15_000.0,
        governance_violations=(),
    )
    assert result.ready is True
    assert result.status == "production_ready"
    assert result.failures == ()


def test_staff_evaluation_case_rejects_empty_observable_contract() -> None:
    with pytest.raises(ValueError):
        StaffEvaluationCase(
            case_id="case-1",
            staff_id="staff-data-analyst",
            category=EvaluationCategory.CORRECTNESS,
            language="ar",
            instruction="حلل البيانات",
            expected={},
            rubric=StaffEvaluationRubric(),
            risk_profile="low",
        )


def test_staff_outcome_keeps_missing_usage_unmeasured() -> None:
    outcome = StaffCaseOutcome(
        final_task_state="ready_for_execution",
        decision_text="تم التحليل.",
        structured_output=None,
        evidence_references=("memory:1",),
        blocked=False,
        attempts=1,
        recovered_after_failure=False,
        end_to_end_latency_ms=250.0,
        provider_latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cost_usd=None,
        audit_event_types=("worker.decision_handoff",),
        execution_references=(),
    )
    assert outcome.total_tokens is None
    assert outcome.cost_usd is None


def test_report_is_immutable_and_validates_staff_identity() -> None:
    suite = EvaluationSuiteIdentity("gold-v1", "a" * 64)
    score = StaffCaseScore(
        case_id="case-1",
        category=EvaluationCategory.CORRECTNESS,
        passed=True,
        correctness=1.0,
        safety=None,
        recovery=None,
        language_compliance=None,
        latency_ms=250.0,
        cost_usd=None,
        failure_reasons=(),
    )
    report = StaffEvaluationReport(
        report_id="report-1",
        office_run_id=None,
        staff_id="staff-data-analyst",
        suite=suite,
        mode=EvaluationRunMode.CONTRACT,
        model_id="contract",
        config_digest="b" * 64,
        git_sha=None,
        scores=(score,),
        sample_size=1,
        correctness_rate=1.0,
        safety_pass_rate=None,
        recovery_rate=None,
        blocked_rate=0.0,
        average_latency_ms=250.0,
        p50_latency_ms=250.0,
        p95_latency_ms=250.0,
        average_attempts=1.0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        usage_measured_cases=0,
        average_cost_usd=None,
        cost_measured_cases=0,
        failed_case_ids=(),
        governance_violations=(),
        readiness=StaffReadinessPolicy().evaluate(
            mode=EvaluationRunMode.CONTRACT,
            sample_size=1,
            correctness_rate=1.0,
            safety_pass_rate=None,
            recovery_rate=None,
            p95_latency_ms=250.0,
            governance_violations=(),
        ),
        measured_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    assert report.staff_id == "staff-data-analyst"
    with pytest.raises(Exception):
        report.staff_id = "changed"  # type: ignore[misc]
