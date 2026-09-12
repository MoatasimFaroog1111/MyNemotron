from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from nemotron.staff.application.tool_ports import CapabilityAuthorityPort, ToolInvocationError
from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.tools import (
    CapabilityToken,
    ToolCategory,
    ToolDefinition,
    ToolExecutionReceipt,
    ToolGatewayError,
    ToolOperation,
)

from .base import GuardedToolAdapter


@dataclass(frozen=True, slots=True)
class OdooToolConfig:
    base_url: str
    database: str
    uid: int
    api_key: str
    allowed_models: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.base_url.strip() or not self.database.strip() or self.uid <= 0 or not self.api_key.strip():
            raise ValueError("Odoo URL, database, uid, and API key are required at runtime.")
        if not self.allowed_models:
            raise ValueError("At least one Odoo model must be allowlisted.")


def odoo_tool_definition(tool_id: str = "odoo") -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        category=ToolCategory.ERP,
        resource="odoo",
        operations=(
            ToolOperation("read_records", ("read",), RiskLevel.LOW, False),
            ToolOperation("create_record", ("write", "create"), RiskLevel.HIGH, True),
            ToolOperation("write_record", ("write",), RiskLevel.HIGH, True),
        ),
    )


class OdooToolAdapter(GuardedToolAdapter):
    def __init__(self, config: OdooToolConfig, capabilities: CapabilityAuthorityPort, *, tool_id: str = "odoo") -> None:
        super().__init__(tool_id, capabilities)
        self._config = config
        self._models = frozenset(config.allowed_models)

    def _model(self, arguments: Mapping[str, Any]) -> str:
        model = str(arguments.get("model", "")).strip()
        if model not in self._models:
            raise ToolGatewayError(f"Odoo model {model!r} is not allowlisted.")
        return model

    def _execute_kw(self, model: str, method: str, args: list[Any], kwargs: Mapping[str, Any], *, mutating: bool) -> Any:
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
                    method,
                    args,
                    dict(kwargs),
                ],
            },
            "id": 1,
        }
        request = urllib.request.Request(
            self._config.base_url.rstrip("/") + "/jsonrpc",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "MyNemotron-Staff-Gateway"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise ToolInvocationError(
                f"Odoo request failed: {type(exc).__name__}",
                side_effect_started=mutating,
            ) from exc
        if not isinstance(result, dict):
            raise ToolInvocationError("Odoo returned an unexpected response shape.", side_effect_started=mutating)
        if result.get("error") is not None:
            raise ToolInvocationError("Odoo JSON-RPC returned an error.", side_effect_started=mutating)
        return result.get("result")

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        capability_token: CapabilityToken,
        idempotency_key: str,
    ) -> ToolExecutionReceipt:
        self._authorize(
            operation,
            arguments,
            capability_token=capability_token,
            idempotency_key=idempotency_key,
        )
        model = self._model(arguments)

        if operation == "read_records":
            domain = arguments.get("domain", [])
            fields = arguments.get("fields", [])
            limit = int(arguments.get("limit", 50))
            if not isinstance(domain, list) or not isinstance(fields, list) or limit < 1 or limit > 200:
                raise ToolGatewayError("Odoo read_records received invalid domain, fields, or limit.")
            result = self._execute_kw(model, "search_read", [domain], {"fields": fields, "limit": limit}, mutating=False)
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=f"odoo:{model}:search_read",
                summary=f"Read Odoo {model} records",
                output=result,
            )

        if operation == "create_record":
            values = arguments.get("values")
            if not isinstance(values, dict) or not values:
                raise ToolGatewayError("Odoo create_record requires a non-empty values object.")
            record_id = self._execute_kw(model, "create", [values], {}, mutating=True)
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=f"odoo:{model}:{record_id}",
                summary=f"Created Odoo {model} record",
                output={"id": record_id},
            )

        if operation == "write_record":
            record_id = int(arguments.get("id", 0))
            values = arguments.get("values")
            if record_id <= 0 or not isinstance(values, dict) or not values:
                raise ToolGatewayError("Odoo write_record requires a positive id and non-empty values object.")
            result = self._execute_kw(model, "write", [[record_id], values], {}, mutating=True)
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=f"odoo:{model}:{record_id}",
                summary=f"Updated Odoo {model} record {record_id}",
                output={"id": record_id, "updated": bool(result)},
            )

        raise ToolGatewayError(f"Unsupported Odoo operation {operation!r}.")
