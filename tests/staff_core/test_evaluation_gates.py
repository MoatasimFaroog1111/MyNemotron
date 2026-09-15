from datetime import datetime, timezone
from pathlib import Path

import pytest

from nemotron.staff.domain.model_routing import TaskClass
from nemotron.staff.evaluation.benchmark import RunnerOutcome, evaluate_model, load_jsonl_cases
from nemotron.staff.evaluation.skill_gate import (
    Capability,
    CapabilityBenchmark,
    CapabilityRegistry,
)


class PerfectRunner:
    def run(self, case):
        return RunnerOutcome(
            output=dict(case.expected),
            latency_ms=250.0,
            cost_usd=0.01,
            attempts=2 if case.inject_failure else 1,
            recovered_after_failure=case.inject_failure,
        )


def test_arabic_holdout_produces_quality_cost_latency_and_recovery_snapshots() -> None:
    cases = load_jsonl_cases(Path("evals/staff/arabic_agent_holdout_v1.jsonl"))
    assert len(cases) == 40

    report = evaluate_model("test-model", PerfectRunner(), cases)
    snapshots = {snapshot.task_class: snapshot for snapshot in report.snapshots()}

    accounting = snapshots[TaskClass.ARABIC_ACCOUNTING]
    planning = snapshots[TaskClass.COMPLEX_PLANNING]
    assert accounting.sample_size == 20
    assert planning.sample_size == 20
    assert accounting.accuracy == 1.0
    assert planning.accuracy == 1.0
    assert accounting.average_latency_ms == 250.0
    assert accounting.average_cost_usd == 0.01
    assert accounting.recovery_rate == 1.0
    assert planning.recovery_rate == 1.0


def test_new_capability_stays_disabled_until_benchmark_and_explicit_approval() -> None:
    registry = CapabilityRegistry()
    assert registry.is_enabled(Capability.INTERACTIVE_BROWSER) is False
    with pytest.raises(PermissionError):
        registry.require_enabled(Capability.INTERACTIVE_BROWSER)

    weak = CapabilityBenchmark(
        capability=Capability.INTERACTIVE_BROWSER,
        correctness=0.94,
        safety_pass_rate=1.0,
        recovery_rate=0.99,
        sample_size=50,
        measured_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError):
        registry.approve(weak, approved_by="reviewer-1", approved_at=weak.measured_at)

    strong = CapabilityBenchmark(
        capability=Capability.INTERACTIVE_BROWSER,
        correctness=0.98,
        safety_pass_rate=1.0,
        recovery_rate=0.99,
        sample_size=50,
        measured_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    registry.approve(strong, approved_by="reviewer-1", approved_at=strong.measured_at)
    assert registry.is_enabled(Capability.INTERACTIVE_BROWSER) is True
