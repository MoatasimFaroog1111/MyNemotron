from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from nemotron.staff.adapters.contract_staff_evaluation import (
    DeterministicContractStaffEvaluationRunner,
)
from nemotron.staff.adapters.jsonl_staff_evaluation import JsonlStaffEvaluationCaseRepository
from nemotron.staff.application.evaluation_scoring import score_case
from nemotron.staff.application.staff_evaluation import RunStaffEvaluation, RunStaffEvaluationRequest
from nemotron.staff.domain.staff_evaluation import EvaluationRunMode, StaffEvaluationReport


NOW = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeAudit:
    def __init__(self) -> None:
        self.events = []

    def append(self, event) -> None:  # type: ignore[no-untyped-def]
        self.events.append(event)


class MemoryReports:
    def __init__(self) -> None:
        self.staff: dict[str, StaffEvaluationReport] = {}
        self.office = {}

    def append_staff(self, report: StaffEvaluationReport) -> None:
        if report.report_id in self.staff:
            raise ValueError("evaluation report already exists")
        self.staff[report.report_id] = report

    def get_staff(self, report_id: str) -> StaffEvaluationReport | None:
        return self.staff.get(report_id)

    def list_staff(self, staff_id=None, *, limit=100):  # type: ignore[no-untyped-def]
        items = tuple(self.staff.values())
        if staff_id is not None:
            items = tuple(item for item in items if item.staff_id == staff_id)
        return items[:limit]

    def append_office(self, report) -> None:  # type: ignore[no-untyped-def]
        self.office[report.run_id] = report

    def get_office(self, run_id):  # type: ignore[no-untyped-def]
        return self.office.get(run_id)

    def list_completed_staff(self, run_id):  # type: ignore[no-untyped-def]
        return tuple(
            sorted(item.staff_id for item in self.staff.values() if item.office_run_id == run_id)
        )


def _canonical_report(report: StaffEvaluationReport) -> bytes:
    payload = asdict(report)
    payload.pop("report_id")
    payload.pop("office_run_id")
    payload.pop("measured_at")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _run_contract_staff(
    *,
    repo: JsonlStaffEvaluationCaseRepository,
    runner: DeterministicContractStaffEvaluationRunner,
    staff_id: str,
    report_id: str,
) -> StaffEvaluationReport:
    reports = MemoryReports()
    evaluator = RunStaffEvaluation(
        cases=repo,
        runner=runner,
        reports=reports,
        clock=FixedClock(),
        audit=FakeAudit(),
    )
    return evaluator(
        RunStaffEvaluationRequest(
            staff_id=staff_id,
            suite_id="gold-v1",
            mode=EvaluationRunMode.CONTRACT,
            report_id=report_id,
            model_id="deterministic-contract",
            config_digest="contract-v1",
            git_sha=None,
        )
    )


def test_all_320_contract_cases_are_network_free_deterministic_and_scorable(monkeypatch) -> None:
    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("contract evaluation attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    repo = JsonlStaffEvaluationCaseRepository(Path("evals"))
    manifest = repo.load_office_manifest()
    runner = DeterministicContractStaffEvaluationRunner()
    first_outcomes = []
    second_outcomes = []
    total = 0

    for staff_id in manifest.staff_ids:
        _, cases = repo.load_suite(staff_id, manifest.suite_id)
        total += len(cases)
        for case in cases:
            first = runner.run(case)
            second = runner.run(case)
            assert first == second
            assert score_case(case, first).passed is True
            first_outcomes.append(asdict(first))
            second_outcomes.append(asdict(second))

    assert total == 320
    assert json.dumps(first_outcomes, ensure_ascii=False, sort_keys=True) == json.dumps(
        second_outcomes,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_contract_reports_are_byte_equivalent_but_never_production_ready(monkeypatch) -> None:
    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("contract evaluation attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    repo = JsonlStaffEvaluationCaseRepository(Path("evals"))
    manifest = repo.load_office_manifest()
    runner = DeterministicContractStaffEvaluationRunner()

    for staff_id in manifest.staff_ids:
        first = _run_contract_staff(
            repo=repo,
            runner=runner,
            staff_id=staff_id,
            report_id=f"contract-a:{staff_id}",
        )
        second = _run_contract_staff(
            repo=repo,
            runner=runner,
            staff_id=staff_id,
            report_id=f"contract-b:{staff_id}",
        )

        assert _canonical_report(first) == _canonical_report(second)
        assert first.sample_size == 20
        assert first.failed_case_ids == ()
        assert first.correctness_rate == 1.0
        assert first.safety_pass_rate == 1.0
        assert first.recovery_rate == 1.0
        assert first.readiness.ready is False
        assert first.readiness.status == "not_ready_insufficient_evidence"
