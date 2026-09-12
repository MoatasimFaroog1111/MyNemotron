from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from nemotron.staff.application.runtime_ports import PlanningContext
from nemotron.staff.domain import RiskLevel
from nemotron.staff.domain.runtime import PlanProposal, PlanStep, RuntimeError


_ALLOWED_TOP_LEVEL = frozenset({"summary", "steps"})
_ALLOWED_STEP_KEYS = frozenset({"step_id", "title", "action", "resource", "risk", "department_id", "depends_on"})
_FORBIDDEN_EXECUTION_KEYS = frozenset({
    "assigned_staff_id",
    "assignee_id",
    "tool",
    "tool_call",
    "execute",
    "approval",
    "approved",
    "credentials",
    "secret",
})


@dataclass(frozen=True, slots=True)
class NemotronPlannerConfig:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: int = 90

    def __post_init__(self) -> None:
        if not self.base_url.strip() or not self.model.strip():
            raise ValueError("Nemotron base_url and model are required.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")


class NemotronPlanningAdapter:
    """OpenAI-compatible Nemotron adapter that can only return non-executing plan proposals."""

    def __init__(self, config: NemotronPlannerConfig) -> None:
        self._config = config

    def propose(self, context: PlanningContext) -> PlanProposal:
        payload = {
            "model": self._config.model,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a planning component inside a governed AI staff system. "
                        "Return JSON only. You may propose work, but you may never select staff, "
                        "call tools, approve actions, execute actions, or include credentials. "
                        "Schema: {summary:string,steps:[{step_id,title,action,resource,risk,"
                        "department_id:null|string,depends_on:[step_id]}]}. "
                        "risk must be one of low, medium, high, critical."
                    ),
                },
                {"role": "user", "content": self._context_json(context)},
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        request = urllib.request.Request(
            self._config.base_url.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                response_payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Nemotron planning request failed safely: {exc}") from exc

        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Nemotron response did not contain a chat-completion message.") from exc
        return self.parse_content(content, context)

    @staticmethod
    def _context_json(context: PlanningContext) -> str:
        data = {
            "organization_id": context.organization_id,
            "goal": {
                "goal_id": context.goal.goal_id,
                "title": context.goal.title,
                "description": context.goal.description,
                "department_id": context.goal.department_id,
            },
            "visible_memory": [
                {
                    "scope": entry.scope.value,
                    "content": entry.content,
                    "source_reference": entry.source_reference,
                }
                for entry in context.visible_memory
            ],
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def parse_content(cls, content: str, context: PlanningContext) -> PlanProposal:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Planner output is not valid JSON.") from exc
        if not isinstance(raw, dict) or set(raw) - _ALLOWED_TOP_LEVEL:
            raise RuntimeError("Planner output contains unsupported top-level fields.")
        if set(raw) & _FORBIDDEN_EXECUTION_KEYS:
            raise RuntimeError("Planner attempted to return execution authority.")
        summary = raw.get("summary")
        raw_steps = raw.get("steps")
        if not isinstance(summary, str) or not summary.strip() or not isinstance(raw_steps, list) or not raw_steps:
            raise RuntimeError("Planner output requires a summary and at least one step.")

        steps: list[PlanStep] = []
        for item in raw_steps:
            if not isinstance(item, dict):
                raise RuntimeError("Every planner step must be an object.")
            keys = set(item)
            if keys - _ALLOWED_STEP_KEYS or keys & _FORBIDDEN_EXECUTION_KEYS:
                raise RuntimeError("Planner step contains a forbidden or unsupported field.")
            try:
                depends_on = item.get("depends_on", [])
                if not isinstance(depends_on, list) or not all(isinstance(value, str) for value in depends_on):
                    raise RuntimeError("Planner depends_on must be a list of step ids.")
                steps.append(
                    PlanStep(
                        step_id=item["step_id"],
                        title=item["title"],
                        action=item["action"],
                        resource=item["resource"],
                        risk=RiskLevel(item["risk"]),
                        department_id=item.get("department_id"),
                        depends_on=tuple(depends_on),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Planner step failed schema validation.") from exc

        return PlanProposal(
            proposal_id=f"plan-{uuid4().hex}",
            goal_id=context.goal.goal_id,
            organization_id=context.organization_id,
            requested_by=context.requested_by,
            summary=summary,
            steps=tuple(steps),
            created_at=datetime.now(timezone.utc),
        )
