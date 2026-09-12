from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
class FilesToolConfig:
    root: Path
    max_read_bytes: int = 1_000_000
    max_write_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if self.max_read_bytes < 1 or self.max_write_bytes < 1:
            raise ValueError("File size limits must be positive.")


def files_tool_definition(tool_id: str = "files") -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        category=ToolCategory.FILES,
        resource="files",
        operations=(
            ToolOperation("read_text", ("read",), RiskLevel.LOW, False),
            ToolOperation("write_text", ("write",), RiskLevel.MEDIUM, True),
        ),
    )


class FilesToolAdapter(GuardedToolAdapter):
    def __init__(self, config: FilesToolConfig, capabilities: CapabilityAuthorityPort, *, tool_id: str = "files") -> None:
        super().__init__(tool_id, capabilities)
        self._config = config
        self._root = config.root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, value: Any) -> Path:
        relative = str(value or "").strip()
        if not relative:
            raise ToolGatewayError("File path is required.")
        candidate = (self._root / relative).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ToolGatewayError("File path escapes the configured gateway root.") from exc
        return candidate

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
        path = self._path(arguments.get("path"))

        if operation == "read_text":
            try:
                size = path.stat().st_size
                if size > self._config.max_read_bytes:
                    raise ToolGatewayError("File exceeds configured read size limit.")
                content = path.read_text(encoding="utf-8")
            except ToolGatewayError:
                raise
            except (OSError, UnicodeError) as exc:
                raise ToolInvocationError(f"File read failed: {type(exc).__name__}", side_effect_started=False) from exc
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(path.relative_to(self._root)),
                summary=f"Read text file {path.relative_to(self._root)}",
                output={"path": str(path.relative_to(self._root)), "content": content},
            )

        if operation == "write_text":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ToolGatewayError("write_text content must be a string.")
            encoded = content.encode("utf-8")
            if len(encoded) > self._config.max_write_bytes:
                raise ToolGatewayError("File exceeds configured write size limit.")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path: str | None = None
            side_effect_started = False
            try:
                with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent) as handle:
                    temp_path = handle.name
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                side_effect_started = True
                os.replace(temp_path, path)
                temp_path = None
            except OSError as exc:
                raise ToolInvocationError(
                    f"File write failed: {type(exc).__name__}",
                    side_effect_started=side_effect_started,
                ) from exc
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(path.relative_to(self._root)),
                summary=f"Wrote text file {path.relative_to(self._root)}",
                output={"path": str(path.relative_to(self._root)), "bytes": len(encoded)},
            )

        raise ToolGatewayError(f"Unsupported files operation {operation!r}.")
