from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .model import StaffCoreError


class ModelRoutingError(StaffCoreError):
    """Raised when no model is validated for a requested task."""


class TaskClass(str, Enum):
    COMPLEX_PLANNING = "complex_planning"
    REPETITIVE_EXECUTION = "repetitive_execution"
    ARABIC_ACCOUNTING = "arabic_accounting"
    DOCUMENT_REASONING = "document_reasoning"


class ModelTier(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_id: str
    provider: str
    tier: ModelTier
    max_context_tokens: int
    enabled: bool = True
    arabic_validated: bool = False

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not self.provider.strip():
            raise ModelRoutingError("Model id and provider are required.")
        if self.max_context_tokens < 1:
            raise ModelRoutingError("max_context_tokens must be positive.")


@dataclass(frozen=True, slots=True)
class BenchmarkSnapshot:
    model_id: str
    task_class: TaskClass
    accuracy: float
    average_latency_ms: float
    average_cost_usd: float
    recovery_rate: float
    sample_size: int
    measured_at: datetime

    def __post_init__(self) -> None:
        if not 0 <= self.accuracy <= 1 or not 0 <= self.recovery_rate <= 1:
            raise ModelRoutingError("Accuracy and recovery_rate must be between 0 and 1.")
        if self.average_latency_ms < 0 or self.average_cost_usd < 0 or self.sample_size < 1:
            raise ModelRoutingError("Benchmark latency/cost cannot be negative and sample_size must be positive.")


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    task_class: TaskClass
    language: str
    estimated_context_tokens: int

    def __post_init__(self) -> None:
        if not self.language.strip() or self.estimated_context_tokens < 0:
            raise ModelRoutingError("Routing request language/context are invalid.")


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    minimum_accuracy: float = 0.90
    minimum_recovery_rate: float = 0.95
    minimum_sample_size: int = 20

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_accuracy <= 1 or not 0 <= self.minimum_recovery_rate <= 1:
            raise ModelRoutingError("Routing quality thresholds must be between 0 and 1.")
        if self.minimum_sample_size < 1:
            raise ModelRoutingError("minimum_sample_size must be positive.")


class BenchmarkModelRouter:
    """Select models from measured quality/cost, never from model-name preference alone."""

    def __init__(self, profiles: tuple[ModelProfile, ...], policy: RoutingPolicy | None = None) -> None:
        self._profiles = {profile.model_id: profile for profile in profiles}
        self._policy = policy or RoutingPolicy()

    def select(self, request: RoutingRequest, snapshots: tuple[BenchmarkSnapshot, ...]) -> ModelProfile:
        expected_tier = (
            ModelTier.PLANNER if request.task_class is TaskClass.COMPLEX_PLANNING else ModelTier.WORKER
        )
        candidates: list[tuple[ModelProfile, BenchmarkSnapshot]] = []
        for snapshot in snapshots:
            profile = self._profiles.get(snapshot.model_id)
            if profile is None or not profile.enabled or profile.tier is not expected_tier:
                continue
            if snapshot.task_class is not request.task_class:
                continue
            if request.estimated_context_tokens > profile.max_context_tokens:
                continue
            if request.language.casefold().startswith("ar") and not profile.arabic_validated:
                continue
            if snapshot.sample_size < self._policy.minimum_sample_size:
                continue
            if snapshot.accuracy < self._policy.minimum_accuracy:
                continue
            if snapshot.recovery_rate < self._policy.minimum_recovery_rate:
                continue
            candidates.append((profile, snapshot))

        if not candidates:
            raise ModelRoutingError(
                f"No validated model meets policy for {request.task_class.value} in {request.language}."
            )

        if expected_tier is ModelTier.PLANNER:
            # Planning prioritizes correctness/recovery, then cost/latency.
            candidates.sort(
                key=lambda item: (
                    -item[1].accuracy,
                    -item[1].recovery_rate,
                    item[1].average_cost_usd,
                    item[1].average_latency_ms,
                    item[0].model_id,
                )
            )
        else:
            # Repetitive work uses the cheapest/fastest model that already cleared quality gates.
            candidates.sort(
                key=lambda item: (
                    item[1].average_cost_usd,
                    item[1].average_latency_ms,
                    -item[1].accuracy,
                    -item[1].recovery_rate,
                    item[0].model_id,
                )
            )
        return candidates[0][0]
