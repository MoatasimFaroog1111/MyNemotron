from datetime import datetime, timedelta, timezone

from nemotron.staff.adapters.sqlite_durable import SQLiteExecutionRepository
from nemotron.staff.domain.durable import ExecutionStatus, RetryPolicy, RuntimeLimits


UTC = timezone.utc


def _at(second: int) -> datetime:
    return datetime(2026, 9, 15, 8, 0, second, tzinfo=UTC)


def test_expired_lease_is_recovered_by_another_worker(tmp_path) -> None:
    repository = SQLiteExecutionRepository(
        tmp_path / "runtime.db",
        limits=RuntimeLimits(max_concurrency=1, lease_seconds=10),
    )
    run = repository.submit(work_item_id="work-1", idempotency_key="work-1:execute", at=_at(0))

    first = repository.claim_next(worker_id="worker-a", at=_at(1))
    assert first is not None
    assert first.run_id == run.run_id
    assert first.attempt == 1

    assert repository.claim_next(worker_id="worker-b", at=_at(5)) is None

    recovered = repository.claim_next(worker_id="worker-b", at=_at(12))
    assert recovered is not None
    assert recovered.run_id == run.run_id
    assert recovered.attempt == 2
    assert recovered.lease_owner == "worker-b"


def test_retry_is_scheduled_with_backoff_and_stops_after_limit(tmp_path) -> None:
    repository = SQLiteExecutionRepository(
        tmp_path / "runtime.db",
        limits=RuntimeLimits(max_concurrency=1, lease_seconds=30),
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=5, max_delay_seconds=60),
    )
    repository.submit(work_item_id="work-1", idempotency_key="work-1:execute", at=_at(0))

    first = repository.claim_next(worker_id="worker-a", at=_at(1))
    assert first is not None
    scheduled = repository.fail(first.run_id, worker_id="worker-a", at=_at(2), error="temporary")
    assert scheduled.status is ExecutionStatus.RETRY_SCHEDULED
    assert scheduled.next_attempt_at == _at(7)

    assert repository.claim_next(worker_id="worker-b", at=_at(6)) is None
    second = repository.claim_next(worker_id="worker-b", at=_at(7))
    assert second is not None
    assert second.attempt == 2

    failed = repository.fail(second.run_id, worker_id="worker-b", at=_at(8), error="still failing")
    assert failed.status is ExecutionStatus.FAILED
    assert failed.next_attempt_at is None
    assert repository.claim_next(worker_id="worker-c", at=_at(40)) is None


def test_global_concurrency_limit_is_enforced_atomically(tmp_path) -> None:
    repository = SQLiteExecutionRepository(
        tmp_path / "runtime.db",
        limits=RuntimeLimits(max_concurrency=2, lease_seconds=30),
    )
    for index in range(3):
        repository.submit(
            work_item_id=f"work-{index}",
            idempotency_key=f"work-{index}:execute",
            at=_at(index),
        )

    first = repository.claim_next(worker_id="worker-a", at=_at(4))
    second = repository.claim_next(worker_id="worker-b", at=_at(4))
    third = repository.claim_next(worker_id="worker-c", at=_at(4))

    assert first is not None
    assert second is not None
    assert third is None

    repository.succeed(first.run_id, worker_id="worker-a", at=_at(5))
    replacement = repository.claim_next(worker_id="worker-c", at=_at(5))
    assert replacement is not None


def test_submit_is_idempotent(tmp_path) -> None:
    repository = SQLiteExecutionRepository(tmp_path / "runtime.db")
    first = repository.submit(work_item_id="work-1", idempotency_key="same-key", at=_at(0))
    second = repository.submit(work_item_id="work-1", idempotency_key="same-key", at=_at(10))

    assert second.run_id == first.run_id
    assert len(repository.list_runs()) == 1
