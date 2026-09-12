from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from nemotron.staff.application.tool_ports import CapabilityAuthorityPort
from nemotron.staff.domain.tools import CapabilityClaims, CapabilityToken, ToolGatewayError


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ToolGatewayError("Capability token contains invalid base64.") from exc


class HMACCapabilityAuthority(CapabilityAuthorityPort):
    """Short-lived signed capability authority. Secret material is injected at runtime only."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("Capability HMAC secret must contain at least 32 bytes.")
        self._secret = secret

    def issue(self, claims: CapabilityClaims) -> CapabilityToken:
        payload = {
            "task_id": claims.task_id,
            "actor_id": claims.actor_id,
            "tool_id": claims.tool_id,
            "operation": claims.operation,
            "arguments_sha256": claims.arguments_sha256,
            "idempotency_key": claims.idempotency_key,
            "issued_at": claims.issued_at.timestamp(),
            "expires_at": claims.expires_at.timestamp(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return CapabilityToken(f"{_encode(raw)}.{_encode(signature)}")

    def verify(self, token: CapabilityToken, *, now: datetime) -> CapabilityClaims:
        try:
            payload_part, signature_part = token.value.split(".", 1)
        except ValueError as exc:
            raise ToolGatewayError("Capability token has invalid format.") from exc
        raw = _decode(payload_part)
        supplied_signature = _decode(signature_part)
        expected_signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ToolGatewayError("Capability token signature is invalid.")
        try:
            payload = json.loads(raw)
            claims = CapabilityClaims(
                task_id=payload["task_id"],
                actor_id=payload["actor_id"],
                tool_id=payload["tool_id"],
                operation=payload["operation"],
                arguments_sha256=payload["arguments_sha256"],
                idempotency_key=payload["idempotency_key"],
                issued_at=datetime.fromtimestamp(float(payload["issued_at"]), timezone.utc),
                expires_at=datetime.fromtimestamp(float(payload["expires_at"]), timezone.utc),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ToolGatewayError("Capability token payload is invalid.") from exc
        instant = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if instant >= claims.expires_at:
            raise ToolGatewayError("Capability token has expired.")
        if instant < claims.issued_at - timedelta(seconds=5):
            raise ToolGatewayError("Capability token is not valid yet.")
        return claims
