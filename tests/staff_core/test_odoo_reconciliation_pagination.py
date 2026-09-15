from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nemotron.staff.adapters.odoo_reconciliation import OdooLedgerEntrySource


def _row(record_id: int) -> dict[str, object]:
    return {
        "id": record_id,
        "date": "2026-09-01",
        "balance": 100.0,
        "currency_id": [1, "SAR"],
        "name": f"ROW-{record_id}",
        "ref": f"TRX-{record_id}",
        "partner_id": [7, "Alpha LLC"],
        "account_id": [42, "101001 Bank"],
        "parent_state": "posted",
        "move_id": [record_id, f"BNK/{record_id}"],
    }


class _PagedReader:
    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[tuple[list[object], int, str | None]] = []

    def search_read(
        self,
        model: str,
        domain: list[object],
        fields: tuple[str, ...],
        *,
        limit: int,
        order: str | None = None,
    ):
        assert model == "account.move.line"
        assert "id" in fields
        cursor = 0
        for clause in domain:
            if isinstance(clause, list) and clause[:2] == ["id", ">"]:
                cursor = int(clause[2])
        self.calls.append((domain, limit, order))
        stop = min(cursor + limit, self.total)
        return [_row(record_id) for record_id in range(cursor + 1, stop + 1)]


def test_odoo_ledger_source_reads_all_rows_beyond_single_page() -> None:
    reader = _PagedReader(total=2505)
    source = OdooLedgerEntrySource(reader)

    rows = source.load_ledger_entries(
        "101001",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )

    assert len(rows) == 2505
    assert rows[0].entry_id == "1"
    assert rows[-1].entry_id == "2505"
    assert rows[-1].amount == Decimal("100.0")
    assert len(reader.calls) == 2
    first_domain, first_limit, first_order = reader.calls[0]
    second_domain, second_limit, second_order = reader.calls[1]
    assert ["id", ">", 2000] not in first_domain
    assert ["id", ">", 2000] in second_domain
    assert first_limit == second_limit == 2000
    assert first_order == second_order == "id asc"


class _StuckReader:
    def search_read(
        self,
        model: str,
        domain: list[object],
        fields: tuple[str, ...],
        *,
        limit: int,
        order: str | None = None,
    ):
        return [_row(record_id) for record_id in range(1, limit + 1)]


def test_odoo_ledger_source_fails_closed_when_pagination_does_not_advance() -> None:
    source = OdooLedgerEntrySource(_StuckReader())

    with pytest.raises(ValueError, match="pagination did not advance"):
        source.load_ledger_entries(
            "101001",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
