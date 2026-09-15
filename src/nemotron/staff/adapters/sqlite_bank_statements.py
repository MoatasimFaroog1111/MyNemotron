from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from nemotron.staff.workflows.bank_reconciliation import BankTransaction


class SQLiteBankStatementRepository:
    """Durable store for normalized bank-statement rows used by draft reconciliation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS bank_statement_transactions (
                    statement_reference TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    transaction_date TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    partner TEXT,
                    source_reference TEXT NOT NULL,
                    PRIMARY KEY(statement_reference, transaction_id)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_bank_statement_date "
                "ON bank_statement_transactions(statement_reference, transaction_date, transaction_id)"
            )

    def replace_statement(
        self,
        statement_reference: str,
        transactions: tuple[BankTransaction, ...],
    ) -> None:
        reference = statement_reference.strip()
        if not reference:
            raise ValueError("Bank statement reference is required.")
        ids = [item.transaction_id for item in transactions]
        if len(ids) != len(set(ids)):
            raise ValueError("Bank statement transaction ids must be unique within a statement.")

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM bank_statement_transactions WHERE statement_reference = ?",
                (reference,),
            )
            db.executemany(
                """INSERT INTO bank_statement_transactions(
                    statement_reference, transaction_id, transaction_date, amount,
                    currency, reference, partner, source_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        reference,
                        item.transaction_id,
                        item.transaction_date.isoformat(),
                        str(item.amount),
                        item.currency,
                        item.reference,
                        item.partner,
                        item.source_reference,
                    )
                    for item in transactions
                ),
            )
            db.commit()

    def load_bank_transactions(self, bank_statement_reference: str) -> tuple[BankTransaction, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT transaction_id, transaction_date, amount, currency,
                          reference, partner, source_reference
                   FROM bank_statement_transactions
                   WHERE statement_reference = ?
                   ORDER BY transaction_date, transaction_id""",
                (bank_statement_reference,),
            ).fetchall()
        return tuple(
            BankTransaction(
                transaction_id=str(row["transaction_id"]),
                transaction_date=date.fromisoformat(str(row["transaction_date"])),
                amount=Decimal(str(row["amount"])),
                currency=str(row["currency"]),
                reference=str(row["reference"]),
                partner=str(row["partner"]) if row["partner"] is not None else None,
                source_reference=str(row["source_reference"]),
            )
            for row in rows
        )
