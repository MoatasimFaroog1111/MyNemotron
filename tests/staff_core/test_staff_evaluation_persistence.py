from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nemotron.staff.adapters.sqlite_staff_evaluation import SQLiteStaffEvaluationReportRepository
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


BASE_TIME = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_staff_report(
    report_id: str,
    staff_id: str,
    *,
    office_run_id: str | None = None,
    measured_at: datetime = BASE_TIME,
) -> StaffEvaluationReport:
    score = StaffCaseScore(
        case_id=f"{staff_id}:correctness:sample-001",
        category=EvaluationCategory.CORRECTNESS,
        passed=True,
        correctness=1.0,
        safety=None,
        recovery=None,
        language_compliance=None,
        latency_ms=123.0,
        cost_usd=None,
        failure_reasons=(),
        blocked=False,
        attempts=1,
        recovered_after_failure=False,
        provider_latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )
    return StaffEvaluationReport(
        report_id=report_id,
        office_run_id=office_run_id,
        staff_id=staff_id,
        suite=EvaluationSuiteIdentity("gold-v1", "a" * 64),
        mode=EvaluationRunMode.LIVE,
        model_id="nemotron-test",
        config_digest="cfg-test",
        git_sha=None,
        scores=(score,),
        sample_size=1,
        correctness_rate=1.0,
        safety_pass_rate=None,
        recovery_rate=None,
        blocked_rate=0.0,
        average_latency_ms=123.0,
        p50_latency_ms=123.0,
        p95_latency_ms=123.0,
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
            "not_ready_insufficient_evidence",
            (ReadinessFailure.INSUFFICIENT_EVIDENCE,),
        ),
        measured_at=measured_at,
    )


def test_staff_report_round_trip_and_json_nulls(tmp_path) -> None:
    path = tmp_path / "evaluation.db"
    repository = SQLiteStaffEvaluationReportRepository(path)
    report = make_staff_report("report-1", "staff-data-analyst")

    repository.append_staff(report)

    assert repository.get_staff(report.report_id) == report
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM staff_evaluation_reports WHERE report_id = ?",
                (report.report_id,),
            ).fetchone()[0]
        )
    assert payload["git_sha"] is None
    assert payload["prompt_tokens"] is None
    assert payload["scores"][0]["provider_latency_ms"] is None
    assert payload["scores"][0]["cost_usd"] is None


def test_duplicate_staff_report_is_rejected_and_history_is_not_overwritten(tmp_path) -> None:
    repository = SQLiteStaffEvaluationReportRepository(tmp_path / "evaluation.db")
    original = make_staff_report("report-1", "staff-data-analyst")
    repository.append_staff(original)
    conflicting = replace(original, model_id="different-model")

    with pytest.raises(ValueError, match="evaluation report already exists"):
        repository.append_staff(conflicting)

    assert repository.get_staff("report-1") == original


def test_staff_reports_are_newest_first_and_filterable(tmp_path) -> None:
    repository = SQLiteStaffEvaluationReportRepository(tmp_path / "evaluation.db")
    older = make_staff_report("older", "staff-data-analyst", measured_at=BASE_TIME)
    newer = make_staff_report(
        "newer",
        "staff-data-analyst",
        measured_at=BASE_TIME + timedelta(minutes=5),
    )
    other = make_staff_report(
        "other",
        "staff-financial-accountant",
        measured_at=BASE_TIME + timedelta(minutes=10),
    )
    repository.append_staff(older)
    repository.append_staff(newer)
    repository.append_staff(other)

    assert tuple(item.report_id for item in repository.list_staff(limit=10)) == (
        "other",
        "newer",
        "older",
    )
    assert tuple(
        item.report_id
        for item in repository.list_staff("staff-data-analyst", limit=10)
    ) == ("newer", "older")
    assert repository.list_staff(limit=2) == (other, newer)


def test_completed_staff_supports_resumable_office_run(tmp_path) -> None:
    repository = SQLiteStaffEvaluationReportRepository(tmp_path / "evaluation.db")
    run_id = "office-run-1"
    repository.append_staff(
        make_staff_report(
            f"{run_id}:staff-financial-accountant",
            "staff-financial-accountant",
            office_run_id=run_id,
        )
    )
    repository.append_staff(
        make_staff_report(
            f"{run_id}:staff-data-analyst",
            "staff-data-analyst",
            office_run_id=run_id,
        )
    )
    repository.append_staff(
        make_staff_report("standalone", "staff-project-manager")
    )

    assert repository.list_completed_staff(run_id) == (
        "staff-data-analyst",
        "staff-financial-accountant",
    )
    assert repository.list_completed_staff("missing-run") == ()


def test_office_report_round_trip_is_append_only(tmp_path) -> None:
    repository = SQLiteStaffEvaluationReportRepository(tmp_path / "evaluation.db")
    first = make_staff_report(
        "office-run-1:staff-data-analyst",
        "staff-data-analyst",
        office_run_id="office-run-1",
    )
    second = make_staff_report(
        "office-run-1:staff-financial-accountant",
        "staff-financial-accountant",
        office_run_id="office-run-1",
    )
    repository.append_staff(first)
    repository.append_staff(second)
    office = OfficeEvaluationReport(
        run_id="office-run-1",
        suite_id="gold-v1",
        dataset_digest="b" * 64,
        mode=EvaluationRunMode.LIVE,
        reports=(first, second),
        measured_at=BASE_TIME + timedelta(minutes=20),
    )

    repository.append_office(office)

    assert repository.get_office(office.run_id) == office
    with pytest.raises(ValueError, match="evaluation report already exists"):
        repository.append_office(office)
    assert repository.get_office(office.run_id) == office
    assert not hasattr(repository, "update_staff")
    assert not hasattr(repository, "delete_staff")
    assert not hasattr(repository, "update_office")
    assert not hasattr(repository, "delete_office")
