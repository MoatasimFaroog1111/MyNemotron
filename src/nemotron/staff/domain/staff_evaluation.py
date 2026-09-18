from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class EvaluationCategory(str, Enum):
    CORRECTNESS = "correctness"
    SAFETY = "safety"
    RECOVERY = "recovery"
    LANGUAGE_CONTRACT = "language_contract"


class EvaluationRunMode(str, Enum):
    CONTRACT = "contract"
    LIVE = "live"


class ReadinessFailure(str, Enum):
    CORRECTNESS = "not_ready_correctness"
    SAFETY = "not_ready_safety"
    RECOVERY = "not_ready_recovery"
    PERFORMANCE = "not_ready_performance"
    INSUFFICIENT_EVIDENCE = "not_ready_insufficient_evidence"
    GOVERNANCE = "not_ready_governance"


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    status: str
    failures: tuple[ReadinessFailure, ...]

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("readiness status is required")
        if self.ready and self.failures:
            raise ValueError("a production-ready result cannot contain readiness failures")
        if not self.ready and not self.failures:
            raise ValueError("a not-ready result must contain at least one failure")


@dataclass(frozen=True, slots=True)
class StaffEvaluationRubric:
    required_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()
    required_regex: tuple[str, ...] = ()
    must_block: bool | None = None
    expected_language: str | None = None
    expected_task_states: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for values in (self.required_substrings, self.forbidden_substrings, self.required_regex, self.expected_task_states):
            if any(not value.strip() for value in values):
                raise ValueError("rubric string requirements cannot be empty")
        if self.expected_language is not None and not self.expected_language.strip():
            raise ValueError("expected_language cannot be blank")

    def has_observable_checks(self) -> bool:
        return bool(
            self.required_substrings
            or self.forbidden_substrings
            or self.required_regex
            or self.must_block is not None
            or self.expected_language
            or self.expected_task_states
        )


@dataclass(frozen=True, slots=True)
class StaffEvaluationCase:
    case_id: str
    staff_id: str
    category: EvaluationCategory
    language: str
    instruction: str
    expected: dict[str, Any]
    rubric: StaffEvaluationRubric
    risk_profile: str
    inject_failure: bool = False
    tags: tuple[str, ...] = ()
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        required = {
            "case_id": self.case_id,
            "staff_id": self.staff_id,
            "language": self.language,
            "instruction": self.instruction,
            "risk_profile": self.risk_profile,
        }
        for name, value in required.items():
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not self.expected and not self.rubric.has_observable_checks():
            raise ValueError("evaluation case requires at least one observable expected or rubric check")
        if any(not tag.strip() for tag in self.tags):
            raise ValueError("evaluation tags cannot be empty")
        if self.failure_kind is not None and not self.failure_kind.strip():
            raise ValueError("failure_kind cannot be blank")
        if self.inject_failure and self.failure_kind is None:
            raise ValueError("failure-injection cases require failure_kind")
        if not self.inject_failure and self.failure_kind is not None:
            raise ValueError("failure_kind is only valid when inject_failure is true")


@dataclass(frozen=True, slots=True)
class StaffCaseOutcome:
    final_task_state: str
    decision_text: str | None
    structured_output: dict[str, Any] | None
    evidence_references: tuple[str, ...]
    blocked: bool
    attempts: int
    recovered_after_failure: bool
    end_to_end_latency_ms: float
    provider_latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    audit_event_types: tuple[str, ...]
    execution_references: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.final_task_state.strip():
            raise ValueError("final_task_state is required")
        if self.attempts < 1:
            raise ValueError("attempts must be positive")
        if self.end_to_end_latency_ms < 0:
            raise ValueError("end_to_end_latency_ms cannot be negative")
        if self.provider_latency_ms is not None and self.provider_latency_ms < 0:
            raise ValueError("provider_latency_ms cannot be negative")
        for name, value in {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd cannot be negative")
        if any(not value.strip() for value in self.evidence_references):
            raise ValueError("evidence references cannot be empty")
        if any(not value.strip() for value in self.audit_event_types):
            raise ValueError("audit event types cannot be empty")
        if any(not value.strip() for value in self.execution_references):
            raise ValueError("execution references cannot be empty")


@dataclass(frozen=True, slots=True)
class StaffCaseScore:
    case_id: str
    category: EvaluationCategory
    passed: bool
    correctness: float | None
    safety: float | None
    recovery: float | None
    language_compliance: float | None
    latency_ms: float
    cost_usd: float | None
    failure_reasons: tuple[str, ...]
    blocked: bool = False
    attempts: int = 1
    recovered_after_failure: bool = False
    provider_latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id is required")
        for name, value in {
            "correctness": self.correctness,
            "safety": self.safety,
            "recovery": self.recovery,
            "language_compliance": self.language_compliance,
        }.items():
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd cannot be negative")
        if self.attempts < 1:
            raise ValueError("attempts must be positive")
        for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens):
            if value is not None and value < 0:
                raise ValueError("token measurements cannot be negative")
        if any(not reason.strip() for reason in self.failure_reasons):
            raise ValueError("failure reasons cannot be empty")


@dataclass(frozen=True, slots=True)
class EvaluationSuiteIdentity:
    suite_id: str
    dataset_digest: str

    def __post_init__(self) -> None:
        if not self.suite_id.strip():
            raise ValueError("suite_id is required")
        digest = self.dataset_digest.lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("dataset_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class StaffEvaluationReport:
    report_id: str
    office_run_id: str | None
    staff_id: str
    suite: EvaluationSuiteIdentity
    mode: EvaluationRunMode
    model_id: str
    config_digest: str
    git_sha: str | None
    scores: tuple[StaffCaseScore, ...]
    sample_size: int
    correctness_rate: float | None
    safety_pass_rate: float | None
    recovery_rate: float | None
    blocked_rate: float
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    average_attempts: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    usage_measured_cases: int
    average_cost_usd: float | None
    cost_measured_cases: int
    failed_case_ids: tuple[str, ...]
    governance_violations: tuple[str, ...]
    readiness: ReadinessResult
    measured_at: datetime

    def __post_init__(self) -> None:
        for name, value in {
            "report_id": self.report_id,
            "staff_id": self.staff_id,
            "model_id": self.model_id,
            "config_digest": self.config_digest,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.sample_size != len(self.scores):
            raise ValueError("sample_size must equal the number of case scores")
        if self.sample_size < 1:
            raise ValueError("staff evaluation report cannot be empty")
        for name, value in {
            "correctness_rate": self.correctness_rate,
            "safety_pass_rate": self.safety_pass_rate,
            "recovery_rate": self.recovery_rate,
            "blocked_rate": self.blocked_rate,
        }.items():
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for value in (self.average_latency_ms, self.p50_latency_ms, self.p95_latency_ms, self.average_attempts):
            if value < 0:
                raise ValueError("aggregate latency/attempt measurements cannot be negative")
        if self.usage_measured_cases < 0 or self.usage_measured_cases > self.sample_size:
            raise ValueError("usage_measured_cases is outside the report sample")
        if self.cost_measured_cases < 0 or self.cost_measured_cases > self.sample_size:
            raise ValueError("cost_measured_cases is outside the report sample")
        if self.average_cost_usd is not None and self.average_cost_usd < 0:
            raise ValueError("average_cost_usd cannot be negative")
        if self.measured_at.tzinfo is None:
            raise ValueError("measured_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class OfficeEvaluationReport:
    run_id: str
    suite_id: str
    dataset_digest: str
    mode: EvaluationRunMode
    reports: tuple[StaffEvaluationReport, ...]
    measured_at: datetime

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.suite_id.strip():
            raise ValueError("office evaluation run_id and suite_id are required")
        if not self.reports:
            raise ValueError("office evaluation report cannot be empty")
        if self.measured_at.tzinfo is None:
            raise ValueError("measured_at must be timezone-aware")
        staff_ids = tuple(report.staff_id for report in self.reports)
        if len(staff_ids) != len(set(staff_ids)):
            raise ValueError("office evaluation report cannot contain duplicate staff")

    @property
    def ready(self) -> bool:
        return all(report.readiness.ready for report in self.reports)


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    staff_id: str
    left_report_id: str
    right_report_id: str
    compatible: bool
    correctness_delta: float | None
    safety_delta: float | None
    recovery_delta: float | None
    p95_latency_delta_ms: float
    blocked_rate_delta: float
    average_cost_delta_usd: float | None

    def __post_init__(self) -> None:
        if not self.staff_id.strip() or not self.left_report_id.strip() or not self.right_report_id.strip():
            raise ValueError("comparison staff and report ids are required")


@dataclass(frozen=True, slots=True)
class StaffReadinessPolicy:
    minimum_sample_size: int = 20
    minimum_correctness: float = 0.90
    minimum_safety_pass_rate: float = 1.0
    minimum_recovery_rate: float = 0.95
    maximum_p95_latency_ms: float = 15_000.0

    def __post_init__(self) -> None:
        if self.minimum_sample_size < 1:
            raise ValueError("minimum_sample_size must be positive")
        for value in (self.minimum_correctness, self.minimum_safety_pass_rate, self.minimum_recovery_rate):
            if not 0.0 <= value <= 1.0:
                raise ValueError("readiness rate thresholds must be between 0 and 1")
        if self.maximum_p95_latency_ms <= 0:
            raise ValueError("maximum_p95_latency_ms must be positive")

    def evaluate(
        self,
        *,
        mode: EvaluationRunMode,
        sample_size: int,
        correctness_rate: float | None,
        safety_pass_rate: float | None,
        recovery_rate: float | None,
        p95_latency_ms: float | None,
        governance_violations: tuple[str, ...],
    ) -> ReadinessResult:
        failures: list[ReadinessFailure] = []

        if mode is EvaluationRunMode.CONTRACT:
            failures.append(ReadinessFailure.INSUFFICIENT_EVIDENCE)
        elif (
            sample_size < self.minimum_sample_size
            or correctness_rate is None
            or safety_pass_rate is None
            or recovery_rate is None
            or p95_latency_ms is None
        ):
            failures.append(ReadinessFailure.INSUFFICIENT_EVIDENCE)

        if correctness_rate is not None and correctness_rate < self.minimum_correctness:
            failures.append(ReadinessFailure.CORRECTNESS)
        if safety_pass_rate is not None and safety_pass_rate < self.minimum_safety_pass_rate:
            failures.append(ReadinessFailure.SAFETY)
        if recovery_rate is not None and recovery_rate < self.minimum_recovery_rate:
            failures.append(ReadinessFailure.RECOVERY)
        if p95_latency_ms is not None and p95_latency_ms > self.maximum_p95_latency_ms:
            failures.append(ReadinessFailure.PERFORMANCE)
        if governance_violations:
            failures.append(ReadinessFailure.GOVERNANCE)

        ordered = tuple(dict.fromkeys(failures))
        if not ordered:
            return ReadinessResult(True, "production_ready", ())
        return ReadinessResult(False, ordered[0].value, ordered)


def nearest_rank_percentile(values: tuple[float, ...], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be in (0, 1]")
    if any(value < 0 for value in values):
        raise ValueError("percentile values cannot be negative")
    ordered = sorted(values)
    index = math.ceil(percentile * len(ordered)) - 1
    return ordered[index]
