from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, Sequence

from nemotron.staff.adapters.tools.odoo import OdooToolConfig
from nemotron.staff.application.tool_ports import ToolInvocationError
from nemotron.staff.workflows.bank_reconciliation import (
    BankReconciliationSource,
    BankTransaction,
    LedgerEntry,
)


class OdooSearchReadPort(Protocol):
    def search_read(
        self,
        model: str,
        domain: list[object],
        fields: tuple[str, ...],
        *,
        limit: int,
        order: str | None = None,
    ) -> Sequence[dict[str, Any]]:
        """Execute a read-only Odoo search_read query."""


class OdooReadClient:
    """Minimal internal Odoo client that exposes search_read only; it cannot mutate Odoo."""

    def __init__(self, config: OdooToolConfig) -> None:
        self._config = config
        self._models = frozenset(config.allowed_models)

    def search_read(
        self,
        model: str,
        domain: list[object],
        fields: tuple[str, ...],
        *,
        limit: int,
        order: str | None = None,
    ) -> Sequence[dict[str, Any]]:
        if model not in self._models:
            raise ValueError(f"Odoo model {model!r} is not allowlisted for read-only reconciliation.")
        if limit < 1 or limit > 5000:
            raise ValueError("Odoo reconciliation read limit must be between 1 and 5000.")
        kwargs: dict[str, Any] = {"fields": list(fields), "limit": limit}
        if order is not None:
            normalized_order = order.strip()
            if not normalized_order:
                raise ValueError("Odoo reconciliation order cannot be empty.")
            kwargs["order"] = normalized_order
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    self._config.database,
                    self._config.uid,
                    self._config.api_key,
                    model,
                    "search_read",
                    [domain],
                    kwargs,
                ],
            },
            "id": 1,
        }
        request = urllib.request.Request(
            self._config.base_url.rstrip("/") + "/jsonrpc",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "MyNemotron-Bank-Reconciliation"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise ToolInvocationError(
                f"Odoo reconciliation read failed: {type(exc).__name__}",
                side_effect_started=False,
            ) from exc
        if not isinstance(result, dict) or result.get("error") is not None:
            raise ToolInvocationError("Odoo reconciliation search_read returned an error.", side_effect_started=False)
        rows = result.get("result")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ToolInvocationError("Odoo reconciliation returned an unexpected response shape.", side_effect_started=False)
        return rows


class OdooLedgerEntrySource:
    """Map complete, read-only account.move.line evidence into normalized reconciliation rows."""

    _PAGE_SIZE = 2000
    _MAX_PAGES = 100
    _FIELDS = (
        "id",
        "date",
        "balance",
        "currency_id",
        "name",
        "ref",
        "partner_id",
        "account_id",
        "parent_state",
        "move_id",
    )

    def __init__(self, reader: OdooSearchReadPort) -> None:
        self._reader = reader

    def load_ledger_entries(
        self,
        bank_account_code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[LedgerEntry, ...]:
        account_code = bank_account_code.strip()
        if not account_code:
            raise ValueError("Bank account code is required.")
        if end_date < start_date:
            raise ValueError("Reconciliation end date cannot precede start date.")

        base_domain: list[object] = [
            ["account_id.code", "=", account_code],
            ["date", ">=", start_date.isoformat()],
            ["date", "<=", end_date.isoformat()],
        ]
        rows = self._read_all_pages(base_domain)
        return tuple(self._map_row(row, account_code) for row in rows)

    def _read_all_pages(self, base_domain: list[object]) -> tuple[dict[str, Any], ...]:
        """Read with deterministic id keyset pagination and fail closed on non-progress."""
        collected: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        last_id = 0

        for _page_number in range(1, self._MAX_PAGES + 1):
            domain = list(base_domain)
            if last_id:
                domain.append(["id", ">", last_id])
            page = tuple(
                self._reader.search_read(
                    "account.move.line",
                    domain,
                    self._FIELDS,
                    limit=self._PAGE_SIZE,
                    order="id asc",
                )
            )
            if not page:
                return tuple(collected)

            previous_id = last_id
            for row in page:
                raw_id = row.get("id")
                try:
                    record_id = int(raw_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Odoo reconciliation page contains a non-numeric record id.") from exc
                if record_id <= previous_id or record_id in seen_ids:
                    raise ValueError(
                        "Odoo reconciliation pagination did not advance monotonically; refusing a partial report."
                    )
                previous_id = record_id
                seen_ids.add(record_id)

            collected.extend(page)
            last_id = previous_id
            if len(page) < self._PAGE_SIZE:
                return tuple(collected)

        raise ValueError(
            "Odoo reconciliation pagination exceeded the safety page limit; refusing a potentially partial report."
        )

    @classmethod
    def _map_row(cls, row: dict[str, Any], account_code: str) -> LedgerEntry:
        record_id = str(row.get("id", "")).strip()
        entry_date = str(row.get("date", "")).strip()
        currency = cls._many2one_name(row.get("currency_id"))
        if not record_id or not entry_date or not currency:
            raise ValueError("Odoo ledger row is missing id, date, or currency evidence.")
        reference = str(row.get("ref") or row.get("name") or cls._many2one_name(row.get("move_id")) or "").strip()
        partner = cls._many2one_name(row.get("partner_id")) or None
        return LedgerEntry(
            entry_id=record_id,
            entry_date=date.fromisoformat(entry_date),
            amount=Decimal(str(row.get("balance", "0"))),
            currency=currency,
            reference=reference,
            partner=partner,
            account_code=account_code,
            posted=str(row.get("parent_state", "")).strip().casefold() == "posted",
            source_reference=f"odoo:account.move.line:{record_id}",
        )

    @staticmethod
    def _many2one_name(value: object) -> str:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return str(value[1]).strip()
        return ""


class ProductionBankReconciliationSource(BankReconciliationSource):
    """Compose durable normalized bank rows with a read-only Odoo ledger source."""

    def __init__(self, bank_statements: object, ledger: OdooLedgerEntrySource) -> None:
        self._bank_statements = bank_statements
        self._ledger = ledger

    def load_bank_transactions(self, bank_statement_reference: str) -> tuple[BankTransaction, ...]:
        loader = getattr(self._bank_statements, "load_bank_transactions")
        return tuple(loader(bank_statement_reference))

    def load_ledger_entries(
        self,
        bank_account_code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[LedgerEntry, ...]:
        return self._ledger.load_ledger_entries(
            bank_account_code,
            start_date=start_date,
            end_date=end_date,
        )
