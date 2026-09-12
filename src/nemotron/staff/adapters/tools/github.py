from __future__ import annotations

import json
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


@dataclass(frozen=True, slots=True)
class GitHubToolConfig:
    token: str | None
    allowed_repositories: tuple[str, ...]
    read_only: bool = True
    api_base: str = "https://api.github.com"

    def __post_init__(self) -> None:
        if not self.allowed_repositories:
            raise ValueError("At least one GitHub repository must be allowlisted.")
        if not self.read_only and not (self.token or "").strip():
            raise ValueError("GitHub token is required when write operations are enabled.")


def github_tool_definition(tool_id: str = "github", *, read_only: bool = False) -> ToolDefinition:
    operations = [
        ToolOperation("get_repository", ("read",), RiskLevel.LOW, False),
        ToolOperation("get_issue", ("read",), RiskLevel.LOW, False),
    ]
    if not read_only:
        operations.extend(
            [
                ToolOperation("create_issue", ("write",), RiskLevel.MEDIUM, True),
                ToolOperation("comment_issue", ("write",), RiskLevel.MEDIUM, True),
            ]
        )
    return ToolDefinition(
        tool_id=tool_id,
        category=ToolCategory.GITHUB,
        resource="github",
        operations=tuple(operations),
    )


class GitHubToolAdapter(GuardedToolAdapter):
    def __init__(
        self,
        config: GitHubToolConfig,
        capabilities: CapabilityAuthorityPort,
        *,
        tool_id: str = "github",
    ) -> None:
        super().__init__(tool_id, capabilities)
        self._config = config
        self._allowed = frozenset(config.allowed_repositories)

    def _repo(self, arguments: Mapping[str, Any]) -> tuple[str, str, str]:
        owner = str(arguments.get("owner", "")).strip()
        repo = str(arguments.get("repo", "")).strip()
        full = f"{owner}/{repo}"
        if not owner or not repo or full not in self._allowed:
            raise ToolGatewayError("GitHub repository is not in the configured allowlist.")
        return owner, repo, full

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None,
        mutating: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if mutating and (self._config.read_only or not (self._config.token or "").strip()):
            raise ToolGatewayError("GitHub adapter is configured read-only.")
        url = self._config.api_base.rstrip("/") + path
        data = None if payload is None else json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MyNemotron-Staff-Gateway",
            "Idempotency-Key": idempotency_key,
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise ToolInvocationError(
                f"GitHub request failed: {type(exc).__name__}",
                side_effect_started=mutating,
            ) from exc
        if not isinstance(value, dict):
            raise ToolInvocationError("GitHub returned an unexpected response shape.", side_effect_started=mutating)
        return value

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
        owner, repo, full = self._repo(arguments)
        escaped_owner = urllib.parse.quote(owner, safe="")
        escaped_repo = urllib.parse.quote(repo, safe="")
        base = f"/repos/{escaped_owner}/{escaped_repo}"

        if operation == "get_repository":
            result = self._request("GET", base, payload=None, mutating=False, idempotency_key=idempotency_key)
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(result.get("html_url") or full),
                summary=f"Read GitHub repository metadata for {full}",
                output={
                    "name": result.get("name"),
                    "full_name": result.get("full_name"),
                    "private": result.get("private"),
                    "default_branch": result.get("default_branch"),
                    "open_issues_count": result.get("open_issues_count"),
                    "updated_at": result.get("updated_at"),
                    "html_url": result.get("html_url"),
                },
            )

        if operation == "get_issue":
            number = int(arguments["issue_number"])
            result = self._request(
                "GET",
                f"{base}/issues/{number}",
                payload=None,
                mutating=False,
                idempotency_key=idempotency_key,
            )
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(result.get("html_url") or f"{full}#{number}"),
                summary=f"Read GitHub issue {full}#{number}",
                output={
                    "number": result.get("number"),
                    "title": result.get("title"),
                    "state": result.get("state"),
                    "body": result.get("body"),
                },
            )

        if operation == "create_issue":
            title = str(arguments.get("title", "")).strip()
            if not title:
                raise ToolGatewayError("GitHub issue title is required.")
            result = self._request(
                "POST",
                f"{base}/issues",
                payload={"title": title, "body": str(arguments.get("body", ""))},
                mutating=True,
                idempotency_key=idempotency_key,
            )
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(result.get("html_url") or result.get("url") or f"{full}#unknown"),
                summary=f"Created GitHub issue in {full}",
                output={"number": result.get("number"), "url": result.get("html_url")},
            )

        if operation == "comment_issue":
            number = int(arguments["issue_number"])
            body = str(arguments.get("body", "")).strip()
            if not body:
                raise ToolGatewayError("GitHub comment body is required.")
            result = self._request(
                "POST",
                f"{base}/issues/{number}/comments",
                payload={"body": body},
                mutating=True,
                idempotency_key=idempotency_key,
            )
            return ToolExecutionReceipt.with_output(
                tool_id=self.tool_id,
                operation=operation,
                reference=str(result.get("html_url") or result.get("url") or f"{full}#{number}"),
                summary=f"Commented on GitHub issue {full}#{number}",
                output={"id": result.get("id"), "url": result.get("html_url")},
            )

        raise ToolGatewayError(f"Unsupported GitHub operation {operation!r}.")
