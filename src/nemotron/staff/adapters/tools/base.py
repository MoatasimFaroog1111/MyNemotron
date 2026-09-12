from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from nemotron.staff.application.tool_ports import CapabilityAuthorityPort
from nemotron.staff.domain.tools import CapabilityClaims, CapabilityToken, ToolGatewayError, arguments_digest


class GuardedToolAdapter:
    """Defense-in-depth base: every concrete adapter independently validates its capability."""

    def __init__(self, tool_id: str, capability_authority: CapabilityAuthorityPort) -> None:
        if not tool_id.strip():
            raise ValueError("tool_id cannot be empty")
        self.tool_id = tool_id
        self._capabilities = capability_authority

    def _authorize(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        capability_token: CapabilityToken,
        idempotency_key: str,
    ) -> CapabilityClaims:
        claims = self._capabilities.verify(capability_token, now=datetime.now(timezone.utc))
        if claims.tool_id != self.tool_id:
            raise ToolGatewayError("Capability token targets a different tool.")
        if claims.operation != operation:
            raise ToolGatewayError("Capability token targets a different operation.")
        if claims.idempotency_key != idempotency_key:
            raise ToolGatewayError("Capability token carries a different idempotency key.")
        if claims.arguments_sha256 != arguments_digest(arguments):
            raise ToolGatewayError("Capability token does not authorize these arguments.")
        return claims
