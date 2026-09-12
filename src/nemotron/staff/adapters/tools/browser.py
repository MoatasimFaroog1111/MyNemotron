from __future__ import annotations

import ipaddress
import urllib.error
import urllib.parse
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "Redirect blocked by browser gateway", headers, fp)


@dataclass(frozen=True, slots=True)
class BrowserToolConfig:
    allowed_hosts: tuple[str, ...]
    max_response_bytes: int = 262_144

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("Browser gateway requires an explicit host allowlist.")
        if self.max_response_bytes < 1 or self.max_response_bytes > 2_000_000:
            raise ValueError("Browser max_response_bytes must be between 1 and 2,000,000.")


def browser_tool_definition(tool_id: str = "browser") -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        category=ToolCategory.BROWSER,
        resource="browser",
        operations=(ToolOperation("get", ("read",), RiskLevel.LOW, False),),
    )


class BrowserToolAdapter(GuardedToolAdapter):
    def __init__(self, config: BrowserToolConfig, capabilities: CapabilityAuthorityPort, *, tool_id: str = "browser") -> None:
        super().__init__(tool_id, capabilities)
        self._config = config
        self._hosts = frozenset(host.lower() for host in config.allowed_hosts)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _validate_url(self, value: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ToolGatewayError("Browser URL must use http or https and include a host.")
        host = parsed.hostname.lower()
        if host not in self._hosts:
            raise ToolGatewayError(f"Browser host {host!r} is not allowlisted.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
            raise ToolGatewayError("Browser gateway refuses private, loopback, link-local, or reserved IP literals.")
        return parsed

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
        if operation != "get":
            raise ToolGatewayError(f"Unsupported browser operation {operation!r}.")
        url = str(arguments.get("url", "")).strip()
        self._validate_url(url)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "MyNemotron-Staff-Gateway/1.0", "Accept": "text/html,text/plain,application/json"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(self._config.max_response_bytes + 1)
                status = getattr(response, "status", 200)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise ToolInvocationError(
                f"Browser GET failed: {type(exc).__name__}",
                side_effect_started=False,
            ) from exc
        if len(raw) > self._config.max_response_bytes:
            raise ToolInvocationError("Browser response exceeded configured size limit.", side_effect_started=False)
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        text = raw.decode(charset, errors="replace")
        return ToolExecutionReceipt.with_output(
            tool_id=self.tool_id,
            operation=operation,
            reference=url,
            summary=f"Fetched {url} with HTTP {status}",
            output={"url": url, "status": status, "content_type": content_type, "body": text},
        )
