from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nemotron.staff.domain.model_routing import BenchmarkSnapshot, TaskClass


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    task_class: TaskClass
    language: str
    prompt: str
    expected: dict[str, Any]
    inject_failure: bool = False

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.language.strip() or not self.prompt.strip():
            raise ValueError("Evaluation case id, language, and prompt are required.")
        if not self.expected:
            raise ValueError("Evaluation case expected output cannot be empty.")


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    output: dict[str, Any]
    latency_ms: float
    cost_usd: float
    attempts: int = 1
    recovered_after_failure: bool = False

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or self.cost_usd < 0 or self.attempts < 1:
            raise ValueError("Runner outcome latency/cost must be non-negative and attempts positive.")


class ModelRunner(Protocol):
    def run(self, case: EvaluationCase) -> RunnerOutcome:
        """Run one case and return structured output plus measured runtime/cost."""


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    task_class: TaskClass
    passed: bool
    score: float
    latency_ms: float
    cost_usd: float
    inject_failure: bool
    recovered_after_failure: bool
    attempts: int


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    model_id: str
    results: tuple[CaseResult, ...]
    measured_at: datetime

    def snapshots(self) -> tuple[BenchmarkSnapshot, ...]:
        snapshots: list[BenchmarkSnapshot] = []
        for task_class in TaskClass:
            subset = tuple(result for result in self.results if result.task_class is task_class)
            if not subset:
                continue
            recovery_cases = tuple(result for result in subset if result.inject_failure)
            recovery_rate = (
                sum(result.recovered_after_failure for result in recovery_cases) / len(recovery_cases)
                if recovery_cases
                else 1.0
            )
            snapshots.append(
                BenchmarkSnapshot(
                    model_id=self.model_id,
                    task_class=task_class,
                    accuracy=sum(result.score for result in subset) / len(subset),
                    average_latency_ms=sum(result.latency_ms for result in subset) / len(subset),
                    average_cost_usd=sum(result.cost_usd for result in subset) / len(subset),
                    recovery_rate=recovery_rate,
                    sample_size=len(subset),
                    measured_at=self.measured_at,
                )
            )
        return tuple(snapshots)


def load_jsonl_cases(path: str | Path) -> tuple[EvaluationCase, ...]:
    cases: list[EvaluationCase] = []
    for line_number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
        cases.append(
            EvaluationCase(
                case_id=str(payload["case_id"]),
                task_class=TaskClass(str(payload["task_class"])),
                language=str(payload.get("language", "ar")),
                prompt=str(payload["prompt"]),
                expected=dict(payload["expected"]),
                inject_failure=bool(payload.get("inject_failure", False)),
            )
        )
    if not cases:
        raise ValueError("Evaluation dataset is empty.")
    return tuple(cases)


def evaluate_model(model_id: str, runner: ModelRunner, cases: tuple[EvaluationCase, ...]) -> BenchmarkReport:
    if not model_id.strip():
        raise ValueError("model_id is required.")
    if not cases:
        raise ValueError("At least one evaluation case is required.")

    results: list[CaseResult] = []
    for case in cases:
        outcome = runner.run(case)
        score = _subset_score(case.expected, outcome.output)
        results.append(
            CaseResult(
                case_id=case.case_id,
                task_class=case.task_class,
                passed=score == 1.0,
                score=score,
                latency_ms=outcome.latency_ms,
                cost_usd=outcome.cost_usd,
                inject_failure=case.inject_failure,
                recovered_after_failure=(outcome.recovered_after_failure if case.inject_failure else True),
                attempts=outcome.attempts,
            )
        )
    return BenchmarkReport(
        model_id=model_id,
        results=tuple(results),
        measured_at=datetime.now(timezone.utc),
    )


def _subset_score(expected: Any, actual: Any) -> float:
    """Score structured outputs by expected-leaf correctness.

    Extra model fields are allowed; every expected leaf must be correct to obtain 1.0.
    """

    total, correct = _count_expected(expected, actual)
    return correct / total if total else 0.0


def _count_expected(expected: Any, actual: Any) -> tuple[int, int]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return _leaf_count(expected), 0
        total = 0
        correct = 0
        for key, expected_value in expected.items():
            if key not in actual:
                total += _leaf_count(expected_value)
                continue
            child_total, child_correct = _count_expected(expected_value, actual[key])
            total += child_total
            correct += child_correct
        return total, correct
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return _leaf_count(expected), 0
        total = 0
        correct = 0
        for index, expected_value in enumerate(expected):
            child_total, child_correct = _count_expected(expected_value, actual[index])
            total += child_total
            correct += child_correct
        return total, correct
    return 1, int(expected == actual)


def _leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_leaf_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_leaf_count(item) for item in value)
    return 1
