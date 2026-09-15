from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date
from decimal import Decimal

from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.http_api import ControlPlaneHTTPServer
from nemotron.staff.control_plane.runtime import build_production_runtime
from nemotron.staff.control_plane.service import ControlPlaneService
from nemotron.staff.workflows.bank_reconciliation import BankTransaction, LedgerEntry


class _InjectedSource:
    def load_bank_transactions(self, bank_statement_reference: str):
        return (
            BankTransaction(
                "bank-1",
                date(2026, 9, 1),
                Decimal("125.50"),
                "SAR",
                "TRX-001",
                "Alpha LLC",
                "statement.pdf:p1:r1",
            ),
        )

    def load_ledger_entries(self, bank_account_code: str, *, start_date: date, end_date: date):
        return (
            LedgerEntry(
                "odoo-11",
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


def _config(tmp_path) -> ControlPlaneConfig:  # type: ignore[no-untyped-def]
    return ControlPlaneConfig(
        data_dir=tmp_path / "data",
        files_root=tmp_path / "files",
        host="127.0.0.1",
        port=8088,
        api_token="control-token-abcdefghijklmnopqrstuvwxyz",
        capability_secret=b"c" * 32,
        nemotron_base_url="https://model.example.test",
        nemotron_model="nemotron-test",
    )


def test_authenticated_api_persists_statement_and_returns_draft_reconciliation(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = build_production_runtime(config, bank_reconciliation_source=_InjectedSource())
    server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneService(runtime), config.api_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        payload = {
            "bank_statement_reference": "statement-2026-09",
            "bank_account_code": "101001",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "actor_id": "finance-reviewer",
            "transactions": [
                {
                    "transaction_id": "bank-1",
                    "transaction_date": "2026-09-01",
                    "amount": "125.50",
                    "currency": "SAR",
                    "reference": "TRX-001",
                    "partner": "Alpha LLC",
                    "source_reference": "statement.pdf:p1:r1",
                }
            ],
        }
        request = urllib.request.Request(
            f"http://{host}:{port}/api/v1/bank-reconciliation/review",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config.api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)

        assert result["matched_count"] == 1
        assert result["unmatched_count"] == 0
        assert result["proposals"][0]["draft_only"] is True
        assert result["proposals"][0]["requires_human_approval"] is True

        persisted = runtime.bank_statements.load_bank_transactions("statement-2026-09")
        assert len(persisted) == 1
        assert persisted[0].transaction_id == "bank-1"
        assert persisted[0].amount == Decimal("125.50")
        assert any(
            event.event_type == "bank_reconciliation.review_generated"
            for event in runtime.audit.list_recent(limit=50)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
