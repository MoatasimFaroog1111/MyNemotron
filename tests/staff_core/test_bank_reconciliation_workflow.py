from datetime import date
from decimal import Decimal

from nemotron.staff.workflows.bank_reconciliation import (
    BankTransaction,
    LedgerEntry,
    MatchStatus,
    ReviewBankReconciliation,
)


class FakeSource:
    def load_bank_transactions(self, bank_statement_reference: str):
        assert bank_statement_reference == "statement-2026-09"
        return (
            BankTransaction(
                "bank-1",
                date(2026, 9, 1),
                Decimal("100.00"),
                "SAR",
                "INV-100",
                "Alpha LLC",
                "bank-pdf:p1:r1",
            ),
            BankTransaction(
                "bank-2",
                date(2026, 9, 2),
                Decimal("250.00"),
                "SAR",
                "TRANSFER-9",
                None,
                "bank-pdf:p1:r2",
            ),
            BankTransaction(
                "bank-3",
                date(2026, 9, 3),
                Decimal("999.00"),
                "SAR",
                "UNKNOWN",
                None,
                "bank-pdf:p1:r3",
            ),
        )

    def load_ledger_entries(self, bank_account_code: str, *, start_date: date, end_date: date):
        assert bank_account_code == "101001"
        assert start_date == date(2026, 9, 1)
        assert end_date == date(2026, 9, 30)
        return (
            LedgerEntry(
                "odoo-1",
                date(2026, 9, 1),
                Decimal("100.00"),
                "SAR",
                "INV-100",
                "Alpha LLC",
                "101001",
                True,
                "odoo:account.move.line:1",
            ),
            LedgerEntry(
                "odoo-2",
                date(2026, 9, 4),
                Decimal("250.00"),
                "SAR",
                "OTHER-REF",
                None,
                "101001",
                True,
                "odoo:account.move.line:2",
            ),
            LedgerEntry(
                "odoo-3",
                date(2026, 9, 5),
                Decimal("400.00"),
                "SAR",
                "DRAFT",
                None,
                "101001",
                False,
                "odoo:account.move.line:3",
            ),
        )


def test_workflow_collects_matches_exceptions_and_review_proposals() -> None:
    report = ReviewBankReconciliation(FakeSource())(
        bank_statement_reference="statement-2026-09",
        bank_account_code="101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    assert report.total_bank_transactions == 3
    assert report.matched_count == 1
    assert report.review_count == 1
    assert report.unmatched_count == 1
    assert report.ambiguous_count == 0

    by_id = {line.bank_transaction_id: line for line in report.lines}
    assert by_id["bank-1"].status is MatchStatus.MATCHED
    assert by_id["bank-1"].selected_ledger_entry_id == "odoo-1"
    assert by_id["bank-2"].status is MatchStatus.REVIEW
    assert by_id["bank-3"].status is MatchStatus.UNMATCHED

    codes = {item.code for item in report.exceptions}
    assert "BANK_TRANSACTION_UNMATCHED" in codes
    assert "UNPOSTED_LEDGER_ENTRY" in codes

    assert report.proposals
    assert all(proposal.draft_only for proposal in report.proposals)
    assert all(proposal.requires_human_approval for proposal in report.proposals)
    assert all(proposal.evidence_references for proposal in report.proposals)


def test_same_amount_with_multiple_strong_candidates_is_never_auto_selected() -> None:
    class AmbiguousSource:
        def load_bank_transactions(self, bank_statement_reference: str):
            return (
                BankTransaction(
                    "bank-1",
                    date(2026, 9, 1),
                    Decimal("100.00"),
                    "SAR",
                    "REF-X",
                    "Vendor",
                    "bank:r1",
                ),
            )

        def load_ledger_entries(self, bank_account_code: str, *, start_date: date, end_date: date):
            return tuple(
                LedgerEntry(
                    f"odoo-{index}",
                    date(2026, 9, 1),
                    Decimal("100.00"),
                    "SAR",
                    "REF-X",
                    "Vendor",
                    "101001",
                    True,
                    f"odoo:{index}",
                )
                for index in (1, 2)
            )

    report = ReviewBankReconciliation(AmbiguousSource())(
        bank_statement_reference="s",
        bank_account_code="101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    assert report.lines[0].status is MatchStatus.AMBIGUOUS
    assert report.lines[0].selected_ledger_entry_id is None
    assert any(item.code == "MULTIPLE_HIGH_CONFIDENCE_CANDIDATES" for item in report.exceptions)
