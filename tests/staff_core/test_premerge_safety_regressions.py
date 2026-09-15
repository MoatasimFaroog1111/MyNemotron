from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nemotron.staff.adapters.sqlite_knowledge import SQLiteKnowledgeRepository
from nemotron.staff.adapters.sqlite_runtime import SQLiteRuntimeStore
from nemotron.staff.adapters.sqlite_worker import SQLiteWorkerQueue
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.durable import RetryPolicy, RuntimeLimits
from nemotron.staff.domain.knowledge import (
    CorrectionStatus,
    KnowledgeCorrection,
    KnowledgeError,
    KnowledgeRecord,
    KnowledgeScope,
    KnowledgeSourceKind,
)
from nemotron.staff.domain.model_routing import (
    BenchmarkModelRouter,
    BenchmarkSnapshot,
    ModelProfile,
    ModelTier,
    TaskClass,
)
from nemotron.staff.domain.runtime import WorkItem, WorkStatus
from nemotron.staff.evaluation.benchmark import BenchmarkReport, CaseResult
from nemotron.staff.workflows.bank_reconciliation import (
    BankTransaction,
    LedgerEntry,
    MatchStatus,
    ReviewBankReconciliation,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _bank(transaction_id: str = "bank-1") -> BankTransaction:
    return BankTransaction(
        transaction_id,
        date(2026, 9, 1),
        Decimal("100.00"),
        "SAR",
        "REF-100",
        "Vendor",
        f"bank:{transaction_id}",
    )


def _ledger(entry_id: str, *, posted: bool = True, reference: str = "REF-100", partner: str | None = "Vendor") -> LedgerEntry:
    return LedgerEntry(
        entry_id,
        date(2026, 9, 1),
        Decimal("100.00"),
        "SAR",
        reference,
        partner,
        "101001",
        posted,
        f"odoo:account.move.line:{entry_id}",
    )


class _Source:
    def __init__(self, bank, ledger) -> None:
        self.bank = tuple(bank)
        self.ledger = tuple(ledger)

    def load_bank_transactions(self, bank_statement_reference: str):
        return self.bank

    def load_ledger_entries(self, bank_account_code: str, *, start_date: date, end_date: date):
        return self.ledger


def _review(bank, ledger):
    return ReviewBankReconciliation(_Source(bank, ledger))(
        bank_statement_reference="statement",
        bank_account_code="101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )


def test_one_ledger_entry_cannot_confirm_two_bank_transactions() -> None:
    report = _review((_bank("bank-1"), _bank("bank-2")), (_ledger("11"),))
    assert report.matched_count == 1
    assert report.lines[0].status is MatchStatus.MATCHED
    assert report.lines[1].status is not MatchStatus.MATCHED
    confirmed = [proposal for proposal in report.proposals if proposal.action == "confirm_existing_match"]
    assert len(confirmed) == 1


def test_unposted_ledger_entry_is_never_a_confirmed_match() -> None:
    report = _review((_bank(),), (_ledger("11", posted=False),))
    assert report.matched_count == 0
    assert report.lines[0].status is not MatchStatus.MATCHED
    assert any(item.code == "UNPOSTED_LEDGER_ENTRY" and item.ledger_entry_id == "11" for item in report.exceptions)
    assert not any(proposal.action == "confirm_existing_match" for proposal in report.proposals)


def test_multiple_reviewable_candidates_are_ambiguous_not_unmatched() -> None:
    ledger = (
        _ledger("11", reference="OTHER-1", partner=None),
        _ledger("12", reference="OTHER-2", partner=None),
    )
    report = _review((_bank(),), ledger)
    assert report.lines[0].status is MatchStatus.AMBIGUOUS
    assert report.lines[0].selected_ledger_entry_id is None
    assert report.unmatched_count == 0


def test_worker_crash_recovery_stops_at_retry_limit(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "worker.sqlite3")
    queue = SQLiteWorkerQueue(
        store,
        limits=RuntimeLimits(max_concurrency=1, lease_seconds=1),
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0, max_delay_seconds=0),
    )
    queue.enqueue(
        WorkItem(
            "work-1", "org-1", "goal-1", "plan-1", "step-1", "Work",
            "read", "crm", RiskLevel.LOW, "worker", NOW,
        )
    )
    assert queue.claim_next("worker", at=NOW) is not None
    assert queue.claim_next("worker", at=NOW + timedelta(seconds=2)) is not None
    assert queue.attempts("work-1") == 2
    assert queue.claim_next("worker", at=NOW + timedelta(seconds=4)) is None
    with sqlite3.connect(store.path) as db:
        status, summary = db.execute(
            "SELECT status, result_summary FROM runtime_work_queue WHERE work_item_id = ?",
            ("work-1",),
        ).fetchone()
    assert status == WorkStatus.BLOCKED.value
    assert "retry limit" in summary.casefold()


def test_stale_knowledge_approval_cannot_overwrite_rejection_or_supersede_target(tmp_path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "knowledge.sqlite3")
    original = KnowledgeRecord(
        "knowledge-1", "org-1", "writer", KnowledgeScope.ORGANIZATION, "Old fact",
        KnowledgeSourceKind.DOCUMENT, "policy:v1", NOW - timedelta(days=2), NOW - timedelta(days=1),
    )
    repository.save_record(original)
    pending = KnowledgeCorrection(
        "correction-1", "org-1", original.knowledge_id, "New fact",
        KnowledgeSourceKind.DOCUMENT, "policy:v2", NOW - timedelta(hours=2),
        "writer", NOW - timedelta(hours=1),
    )
    repository.save_correction(pending)
    rejected = pending.decide(approver_id="approver-a", approved=False, at=NOW, rationale="reject")
    repository.save_correction(rejected)
    stale_approval = pending.decide(
        approver_id="approver-b", approved=True, at=NOW + timedelta(seconds=1), rationale="stale approve"
    )
    replacement = KnowledgeRecord(
        "knowledge-2", "org-1", "writer", KnowledgeScope.ORGANIZATION, "New fact",
        KnowledgeSourceKind.DOCUMENT, "policy:v2", NOW - timedelta(hours=2), NOW + timedelta(seconds=1),
    )
    with pytest.raises(KnowledgeError):
        repository.apply_approved_correction(stale_approval, target=original, replacement=replacement)
    assert repository.get_correction("correction-1").status is CorrectionStatus.REJECTED
    assert repository.get_record("knowledge-1").is_active
    with pytest.raises(LookupError):
        repository.get_record("knowledge-2")


def test_benchmark_without_failure_cases_does_not_claim_perfect_recovery() -> None:
    report = BenchmarkReport(
        model_id="model-a",
        results=(CaseResult("ordinary", TaskClass.ARABIC_ACCOUNTING, True, 1.0, 10, 0.01, False, True, 1),),
        measured_at=NOW,
    )
    assert report.snapshots()[0].recovery_rate == 0.0


def _config(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    values = {
        "data_dir": tmp_path / "data",
        "files_root": tmp_path / "files",
        "host": "127.0.0.1",
        "port": 8088,
        "api_token": "control-token-abcdefghijklmnopqrstuvwxyz",
        "capability_secret": b"c" * 32,
        "nemotron_base_url": "https://model.example.test",
        "nemotron_model": "legacy-model",
    }
    values.update(overrides)
    return ControlPlaneConfig(**values)


def test_production_browser_is_blocked_until_capability_gate_is_approved(tmp_path) -> None:
    runtime = build_production_runtime(_config(tmp_path, browser_allowed_hosts=("example.com",)))
    assert "browser" in runtime.registered_tools
    with pytest.raises(PermissionError, match="disabled until"):
        runtime.tools.definition("browser")


def _snapshot(model_id: str, task_class: TaskClass) -> BenchmarkSnapshot:
    return BenchmarkSnapshot(model_id, task_class, 0.99, 100, 0.01, 0.99, 40, NOW)


def test_production_runtime_can_route_planner_and_worker_from_benchmarks(tmp_path) -> None:
    profiles = (
        ModelProfile("planner-routed", "provider", ModelTier.PLANNER, 100_000, arabic_validated=True),
        ModelProfile("worker-routed", "provider", ModelTier.WORKER, 100_000, arabic_validated=True),
    )
    router = BenchmarkModelRouter(profiles)
    snapshots = (
        _snapshot("planner-routed", TaskClass.COMPLEX_PLANNING),
        _snapshot("worker-routed", TaskClass.ARABIC_ACCOUNTING),
    )
    runtime = build_production_runtime(
        _config(tmp_path), model_router=router, benchmark_snapshots=snapshots
    )
    assert runtime.planner._config.model == "planner-routed"
    assert runtime.reasoner._config.model == "worker-routed"
