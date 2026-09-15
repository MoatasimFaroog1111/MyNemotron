from datetime import datetime, timezone

import pytest

from nemotron.staff.domain.model_routing import (
    BenchmarkModelRouter,
    BenchmarkSnapshot,
    ModelProfile,
    ModelRoutingError,
    ModelTier,
    RoutingRequest,
    TaskClass,
)


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def _snapshot(model_id: str, task_class: TaskClass, *, accuracy: float, cost: float, latency: float):
    return BenchmarkSnapshot(
        model_id=model_id,
        task_class=task_class,
        accuracy=accuracy,
        average_latency_ms=latency,
        average_cost_usd=cost,
        recovery_rate=0.99,
        sample_size=50,
        measured_at=NOW,
    )


def test_planner_prefers_higher_accuracy_before_cost() -> None:
    profiles = (
        ModelProfile("planner-strong", "provider-a", ModelTier.PLANNER, 200_000, arabic_validated=True),
        ModelProfile("planner-cheap", "provider-b", ModelTier.PLANNER, 200_000, arabic_validated=True),
    )
    router = BenchmarkModelRouter(profiles)
    selected = router.select(
        RoutingRequest(TaskClass.COMPLEX_PLANNING, "ar", 20_000),
        (
            _snapshot("planner-strong", TaskClass.COMPLEX_PLANNING, accuracy=0.98, cost=0.20, latency=2500),
            _snapshot("planner-cheap", TaskClass.COMPLEX_PLANNING, accuracy=0.94, cost=0.02, latency=900),
        ),
    )
    assert selected.model_id == "planner-strong"


def test_worker_prefers_cheaper_model_after_quality_gate() -> None:
    profiles = (
        ModelProfile("worker-fast", "provider-a", ModelTier.WORKER, 100_000, arabic_validated=True),
        ModelProfile("worker-expensive", "provider-b", ModelTier.WORKER, 100_000, arabic_validated=True),
    )
    router = BenchmarkModelRouter(profiles)
    selected = router.select(
        RoutingRequest(TaskClass.ARABIC_ACCOUNTING, "ar", 5_000),
        (
            _snapshot("worker-fast", TaskClass.ARABIC_ACCOUNTING, accuracy=0.93, cost=0.01, latency=500),
            _snapshot("worker-expensive", TaskClass.ARABIC_ACCOUNTING, accuracy=0.97, cost=0.08, latency=1200),
        ),
    )
    assert selected.model_id == "worker-fast"


def test_unvalidated_arabic_model_is_rejected_even_with_good_numbers() -> None:
    router = BenchmarkModelRouter(
        (ModelProfile("candidate", "provider", ModelTier.WORKER, 1_000_000, arabic_validated=False),)
    )
    with pytest.raises(ModelRoutingError):
        router.select(
            RoutingRequest(TaskClass.ARABIC_ACCOUNTING, "ar-SA", 10_000),
            (_snapshot("candidate", TaskClass.ARABIC_ACCOUNTING, accuracy=0.99, cost=0.01, latency=100),),
        )
