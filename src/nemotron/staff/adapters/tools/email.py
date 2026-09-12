from __future__ import annotations

import hashlib
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
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
class SMTPToolConfig:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    allowed_recipient_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.host.strip() or not self.sender.strip() or self.port <= 0:
            raise ValueError("SMTP host, port, and sender are required.")


def email_tool_definition(tool_id: str = "email") -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        category=ToolCategory.EMAIL,
        resource="email",
        operations=(ToolOperation("send_email", ("send", "write"), RiskLevel.MEDIUM, True),),
    )


class SMTPEmailToolAdapter(GuardedToolAdapter):
    def __init__(self, config: SMTPToolConfig, capabilities: CapabilityAuthorityPort, *, tool_id: str = "email") -> None:
        super().__init__(tool_id, capabilities)
        self._config = config
        self._domains = frozenset(domain.lower() for domain in config.allowed_recipient_domains)

    def _validate_recipients(self, recipients: list[str]) -> None:
        if not recipients:
            raise ToolGatewayError("Email requires at least one recipient.")
        if not self._domains:
            return
        for address in recipients:
            _, separator, domain = address.rpartition("@")
            if not separator or domain.lower() not in self._domains:
                raise ToolGatewayError(f"Recipient domain is not allowlisted for {address!r}.")

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
        if operation != "send_email":
            raise ToolGatewayError(f"Unsupported email operation {operation!r}.")

        raw_recipients = arguments.get("to")
        if isinstance(raw_recipients, str):
            recipients = [raw_recipients]
        elif isinstance(raw_recipients, list) and all(isinstance(value, str) for value in raw_recipients):
            recipients = list(raw_recipients)
        else:
            raise ToolGatewayError("Email 'to' must be a string or list of strings.")
        self._validate_recipients(recipients)
        subject = str(arguments.get("subject", "")).strip()
        body = str(arguments.get("body", ""))
        if not subject:
            raise ToolGatewayError("Email subject is required.")

        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        message_id = f"<{digest}@mynemotron.local>"
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message.set_content(body)

        side_effect_started = False
        try:
            with smtplib.SMTP(self._config.host, self._config.port, timeout=30) as smtp:
                if self._config.starttls:
                    smtp.starttls()
                if self._config.username:
                    smtp.login(self._config.username, self._config.password or "")
                side_effect_started = True
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise ToolInvocationError(
                f"SMTP send failed: {type(exc).__name__}",
                side_effect_started=side_effect_started,
            ) from exc

        return ToolExecutionReceipt.with_output(
            tool_id=self.tool_id,
            operation=operation,
            reference=message_id,
            summary=f"Sent email to {len(recipients)} recipient(s)",
            output={"message_id": message_id, "recipients": recipients},
        )
