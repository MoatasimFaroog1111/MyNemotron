from __future__ import annotations

from datetime import date
from decimal import Decimal

from nemotron.staff.adapters.odoo_reconciliation import OdooLedgerEntrySource
from nemotron.staff.adapters.sqlite_bank_statements import SQLiteBankStatementRepository
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.workflows.bank_reconciliation import BankTransaction, LedgerEntry


def _config(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    values = {
        "data_dir": tmp_path / "data",
        "files_root": tmp_path / "files",
        "host": "127.0.0.1",
        "port": 8088,
        "api_token": "control-token-abcdefghijklmnopqrstuvwxyz",
        "capability_secret": b"c" * 32,
        "nemotron_base_url": "https://model.example.test",
        "nemotron_model": "nemotron-test",
    }
    values.update(overrides)
    return ControlPlaneConfig(**values)


def test_runtime_wires_worker_durability_policy_from_config(tmp_path) -> None:
    runtime = build_production_runtime(
        _config(
            tmp_path,
            worker_max_attempts=6,
            worker_max_concurrency=2,
            worker_lease_seconds=41,
            worker_retry_initial_seconds=7,
            worker_retry_max_seconds=70,
            worker_retry_backoff_factor=3.0,
        )
    )

    assert runtime.worker_queue.limits.max_concurrency == 2
    assert runtime.worker_queue.limits.lease_seconds == 41
    assert runtime.worker_queue.retry_policy.max_attempts == 6
    assert runtime.worker_queue.retry_policy.initial_delay_seconds == 7
    assert runtime.worker_queue.retry_policy.max_delay_seconds == 70
    assert runtime.worker_queue.retry_policy.backoff_factor == 3.0
    assert runtime.worker._max_attempts == 6


def test_sqlite_bank_statement_repository_survives_recreation(tmp_path) -> None:
    path = tmp_path / "bank.sqlite3"
    repository = SQLiteBankStatementRepository(path)
    transactions = (
        BankTransaction(
            transaction_id="bank-1",
            transaction_date=date(2026, 9, 1),
            amount=Decimal("125.50"),
            currency="SAR",
            reference="TRX-001",
            partner="Alpha LLC",
            source_reference="statement.pdf:p1:r1",
        ),
    )

    repository.replace_statement("statement-2026-09", transactions)

    reloaded = SQLiteBankStatementRepository(path).load_bank_transactions("statement-2026-09")
    assert reloaded == transactions


class _FakeOdooReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object], tuple[str, ...], int, str | None]] = []

    def search_read(
        self,
        model: str,
        domain: list[object],
        fields: tuple[str, ...],
        *,
        limit: int,
        order: str | None = None,
    ):
        self.calls.append((model, domain, fields, limit, order))
        return [
            {
                "id": 11,
                "date": "2026-09-01",
                "balance": 125.5,
                "currency_id": [1, "SAR"],
                "name": "INV-100",
                "ref": "TRX-001",
                "partner_id": [7, "Alpha LLC"],
                "account_id": [42, "101001 Bank"],
                "parent_state": "posted",
                "move_id": [99, "BNK1/2026/001"],
            }
        ]


def test_odoo_ledger_source_reads_account_move_line_without_mutation() -> None:
    reader = _FakeOdooReader()
    source = OdooLedgerEntrySource(reader)

    rows = source.load_ledger_entries(
        "101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    assert rows == (
        LedgerEntry(
            entry_id="11",
            entry_date=date(2026, 9, 1),
            amount=Decimal("125.5"),
            currency="SAR",
            reference="TRX-001",
            partner="Alpha LLC",
            account_code="101001",
            posted=True,
            source_reference="odoo:account.move.line:11",
        ),
    )
    model, domain, fields, limit, order = reader.calls[0]
    assert model == "account.move.line"
    assert ["account_id.code", "=", "101001"] in domain
    assert ["date", ">=", "2026-09-01"] in domain
    assert ["date", "<=", "2026-09-30"] in domain
    assert "balance" in fields
    assert limit == 2000
    assert order == "id asc"


class _InjectedReconciliationSource:
    def load_bank_transactions(self, bank_statement_reference: str):
        return (
            BankTransaction(
                "bank-1",
                date(2026, 9, 1),
                Decimal("125.50"),
                "SAR",
                "TRX-001",
                "Alpha LLC",
                "statement:p1:r1",
            ),
        )

    def load_ledger_entries(self, bank_account_code: str, *, start_date: date, end_date: date):
        return (
            LedgerEntry(
                "11",
                date(2026, 9, 1),
                Decimal("125.50"),
                "SAR",
                "TRX-001",
                "Alpha LLC",
                bank_account_code,
                True,
                "odoo:account.move.line:11",
            ),
        )


def test_runtime_exposes_draft_only_bank_reconciliation_service(tmp_path) -> None:
    runtime = build_production_runtime(
        _config(tmp_path),
        bank_reconciliation_source=_InjectedReconciliationSource(),
    )

    report = runtime.review_bank_reconciliation(
        bank_statement_reference="statement-2026-09",
        bank_account_code="101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    assert report.matched_count == 1
    assert report.proposals
    assert all(proposal.draft_only for proposal in report.proposals)
    assert all(proposal.requires_human_approval for proposal in report.proposals)


def test_runtime_builds_reconciliation_from_odoo_when_read_model_is_allowlisted(tmp_path) -> None:
    runtime = build_production_runtime(
        _config(
            tmp_path,
            odoo_base_url="https://odoo.example.test",
            odoo_database="company-db",
            odoo_uid=7,
            odoo_api_key="test-api-key",
            odoo_allowed_models=("account.move.line",),
        )
    )

    assert runtime.bank_statements is not None
    assert runtime.review_bank_reconciliation is not None
    assert runtime.health()["bank_reconciliation"] is True
