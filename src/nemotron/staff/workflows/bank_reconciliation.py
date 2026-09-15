from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Protocol


class MatchStatus(str, Enum):
    MATCHED = "matched"
    REVIEW = "review"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class BankTransaction:
    transaction_id: str
    transaction_date: date
    amount: Decimal
    currency: str
    reference: str
    partner: str | None
    source_reference: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    entry_date: date
    amount: Decimal
    currency: str
    reference: str
    partner: str | None
    account_code: str
    posted: bool
    source_reference: str


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    ledger_entry_id: str
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationLine:
    bank_transaction_id: str
    status: MatchStatus
    selected_ledger_entry_id: str | None
    candidates: tuple[MatchCandidate, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class ReconciliationException:
    code: str
    severity: str
    message: str
    bank_transaction_id: str | None = None
    ledger_entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationProposal:
    proposal_id: str
    action: str
    summary: str
    evidence_references: tuple[str, ...]
    draft_only: bool = True
    requires_human_approval: bool = True


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    bank_statement_reference: str
    bank_account_code: str
    total_bank_transactions: int
    total_ledger_entries: int
    matched_count: int
    review_count: int
    unmatched_count: int
    ambiguous_count: int
    lines: tuple[ReconciliationLine, ...]
    exceptions: tuple[ReconciliationException, ...]
    proposals: tuple[ReconciliationProposal, ...]

    def to_review_dict(self) -> dict[str, object]:
        """Return a JSON-serializable review artifact; never executes accounting writes."""
        return asdict(self)


class BankReconciliationSource(Protocol):
    def load_bank_transactions(self, bank_statement_reference: str) -> tuple[BankTransaction, ...]:
        """Read normalized bank-statement transactions."""

    def load_ledger_entries(
        self,
        bank_account_code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[LedgerEntry, ...]:
        """Read normalized Odoo/general-ledger entries for the same period."""


class ReviewBankReconciliation:
    """Evidence-first, draft-only bank reconciliation.

    This workflow reads data, scores candidates, explains exceptions, and prepares
    review proposals. It deliberately has no posting/write dependency.
    """

    def __init__(self, source: BankReconciliationSource) -> None:
        self._source = source

    def __call__(
        self,
        *,
        bank_statement_reference: str,
        bank_account_code: str,
        start_date: date,
        end_date: date,
    ) -> ReconciliationReport:
        bank = self._source.load_bank_transactions(bank_statement_reference)
        ledger = self._source.load_ledger_entries(
            bank_account_code,
            start_date=start_date,
            end_date=end_date,
        )

        lines: list[ReconciliationLine] = []
        exceptions: list[ReconciliationException] = []
        proposals: list[ReconciliationProposal] = []
        selected_ledger_ids: set[str] = set()

        for transaction in bank:
            # Only posted, still-unconsumed entries may become automatic match candidates.
            # Draft entries remain visible through explicit exceptions below, never as final evidence.
            candidates = tuple(
                sorted(
                    (
                        candidate
                        for entry in ledger
                        if entry.posted
                        and entry.entry_id not in selected_ledger_ids
                        and (candidate := self._score(transaction, entry)) is not None
                    ),
                    key=lambda candidate: (-candidate.score, candidate.ledger_entry_id),
                )
            )

            strong = tuple(candidate for candidate in candidates if candidate.score >= 0.85)
            reviewable = tuple(candidate for candidate in candidates if candidate.score >= 0.65)

            if len(strong) == 1:
                chosen = strong[0]
                status = MatchStatus.MATCHED
                explanation = "High-confidence match based on amount plus corroborating date/reference/partner evidence."
                selected_ledger_ids.add(chosen.ledger_entry_id)
                proposals.append(
                    ReconciliationProposal(
                        proposal_id=f"confirm:{transaction.transaction_id}:{chosen.ledger_entry_id}",
                        action="confirm_existing_match",
                        summary=(
                            f"Confirm bank transaction {transaction.transaction_id} against ledger entry "
                            f"{chosen.ledger_entry_id}; score={chosen.score:.2f}."
                        ),
                        evidence_references=(
                            transaction.source_reference,
                            self._ledger_by_id(ledger, chosen.ledger_entry_id).source_reference,
                        ),
                    )
                )
                selected = chosen.ledger_entry_id
            elif len(strong) > 1:
                status = MatchStatus.AMBIGUOUS
                selected = None
                explanation = "More than one high-confidence ledger candidate exists; automatic selection is unsafe."
                exceptions.append(
                    ReconciliationException(
                        code="MULTIPLE_HIGH_CONFIDENCE_CANDIDATES",
                        severity="high",
                        message=explanation,
                        bank_transaction_id=transaction.transaction_id,
                    )
                )
                proposals.append(
                    ReconciliationProposal(
                        proposal_id=f"review-ambiguous:{transaction.transaction_id}",
                        action="review_ambiguous_match",
                        summary=f"Review {len(strong)} high-confidence candidates before any accounting action.",
                        evidence_references=(transaction.source_reference,),
                    )
                )
            elif len(reviewable) == 1:
                chosen = reviewable[0]
                status = MatchStatus.REVIEW
                selected = chosen.ledger_entry_id
                explanation = "A plausible candidate exists, but evidence is below the auto-match threshold."
                proposals.append(
                    ReconciliationProposal(
                        proposal_id=f"review:{transaction.transaction_id}:{chosen.ledger_entry_id}",
                        action="review_candidate_match",
                        summary=(
                            f"Review bank transaction {transaction.transaction_id} against ledger entry "
                            f"{chosen.ledger_entry_id}; score={chosen.score:.2f}."
                        ),
                        evidence_references=(
                            transaction.source_reference,
                            self._ledger_by_id(ledger, chosen.ledger_entry_id).source_reference,
                        ),
                    )
                )
            elif len(reviewable) > 1:
                status = MatchStatus.AMBIGUOUS
                selected = None
                explanation = "More than one plausible ledger candidate exists; human review is required."
                exceptions.append(
                    ReconciliationException(
                        code="MULTIPLE_REVIEW_CANDIDATES",
                        severity="medium",
                        message=explanation,
                        bank_transaction_id=transaction.transaction_id,
                    )
                )
                proposals.append(
                    ReconciliationProposal(
                        proposal_id=f"review-ambiguous:{transaction.transaction_id}",
                        action="review_ambiguous_match",
                        summary=f"Review {len(reviewable)} plausible candidates before any accounting action.",
                        evidence_references=(transaction.source_reference,),
                    )
                )
            else:
                status = MatchStatus.UNMATCHED
                selected = None
                explanation = "No posted, unused ledger entry met the minimum review threshold."
                exceptions.append(
                    ReconciliationException(
                        code="BANK_TRANSACTION_UNMATCHED",
                        severity="medium",
                        message=explanation,
                        bank_transaction_id=transaction.transaction_id,
                    )
                )
                proposals.append(
                    ReconciliationProposal(
                        proposal_id=f"investigate:{transaction.transaction_id}",
                        action="investigate_unmatched_bank_transaction",
                        summary=(
                            "Investigate the bank transaction and, only after evidence review, prepare a draft "
                            "journal-entry suggestion if a genuine missing book entry is proven."
                        ),
                        evidence_references=(transaction.source_reference,),
                    )
                )

            lines.append(
                ReconciliationLine(
                    bank_transaction_id=transaction.transaction_id,
                    status=status,
                    selected_ledger_entry_id=selected,
                    candidates=candidates[:5],
                    explanation=explanation,
                )
            )

        for entry in ledger:
            if not entry.posted:
                exceptions.append(
                    ReconciliationException(
                        code="UNPOSTED_LEDGER_ENTRY",
                        severity="medium",
                        message="Ledger candidate is still draft/unposted and cannot be treated as final evidence.",
                        ledger_entry_id=entry.entry_id,
                    )
                )

        return ReconciliationReport(
            bank_statement_reference=bank_statement_reference,
            bank_account_code=bank_account_code,
            total_bank_transactions=len(bank),
            total_ledger_entries=len(ledger),
            matched_count=sum(line.status is MatchStatus.MATCHED for line in lines),
            review_count=sum(line.status is MatchStatus.REVIEW for line in lines),
            unmatched_count=sum(line.status is MatchStatus.UNMATCHED for line in lines),
            ambiguous_count=sum(line.status is MatchStatus.AMBIGUOUS for line in lines),
            lines=tuple(lines),
            exceptions=tuple(exceptions),
            proposals=tuple(proposals),
        )

    @classmethod
    def _score(cls, transaction: BankTransaction, entry: LedgerEntry) -> MatchCandidate | None:
        if transaction.currency.upper() != entry.currency.upper() or transaction.amount != entry.amount:
            return None

        score = 0.55
        reasons = ["same amount", "same currency"]

        days = abs((transaction.transaction_date - entry.entry_date).days)
        if days == 0:
            score += 0.20
            reasons.append("same date")
        elif days <= 1:
            score += 0.15
            reasons.append("date within 1 day")
        elif days <= 3:
            score += 0.10
            reasons.append("date within 3 days")
        elif days <= 7:
            score += 0.05
            reasons.append("date within 7 days")

        bank_ref = cls._normalize(transaction.reference)
        ledger_ref = cls._normalize(entry.reference)
        if bank_ref and ledger_ref:
            if bank_ref == ledger_ref:
                score += 0.20
                reasons.append("same reference")
            elif bank_ref in ledger_ref or ledger_ref in bank_ref:
                score += 0.12
                reasons.append("reference overlap")

        bank_partner = cls._normalize(transaction.partner or "")
        ledger_partner = cls._normalize(entry.partner or "")
        if bank_partner and ledger_partner and bank_partner == ledger_partner:
            score += 0.15
            reasons.append("same partner")

        return MatchCandidate(entry.entry_id, min(score, 1.0), tuple(reasons))

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    @staticmethod
    def _ledger_by_id(entries: tuple[LedgerEntry, ...], entry_id: str) -> LedgerEntry:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise LookupError(entry_id)
