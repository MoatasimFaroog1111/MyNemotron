from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from nemotron.staff.application.direct_instructions import SubmitDirectInstruction, SubmitDirectInstructionRequest
from nemotron.staff.application.tool_ports import ToolAdapterPort, ToolRegistryPort
from nemotron.staff.domain.runtime import WorkItem
from nemotron.staff.domain.tools import ToolDefinition
from nemotron.staff.evaluation.skill_gate import Capability, CapabilityRegistry


class GatedToolRegistry(ToolRegistryPort):
    """Wrap a tool registry so unapproved capabilities remain unreachable at runtime."""

    def __init__(
        self,
        inner: ToolRegistryPort,
        capability_registry: CapabilityRegistry,
        gated_tools: Mapping[str, Capability] | None = None,
    ) -> None:
        self._inner = inner
        self._capabilities = capability_registry
        self._gated_tools = dict(
            gated_tools
            or {
                "browser": Capability.INTERACTIVE_BROWSER,
                "documents": Capability.DOCUMENT_UNDERSTANDING,
            }
        )

    def _require(self, tool_id: str) -> None:
        capability = self._gated_tools.get(tool_id)
        if capability is not None:
            self._capabilities.require_enabled(capability)

    def definition(self, tool_id: str) -> ToolDefinition:
        self._require(tool_id)
        return self._inner.definition(tool_id)

    def adapter(self, tool_id: str) -> ToolAdapterPort:
        self._require(tool_id)
        return self._inner.adapter(tool_id)


@dataclass(frozen=True, slots=True)
class VoiceTranscript:
    staff_id: str
    transcript: str
    actor_id: str = "voice-operator"


class GovernedVoiceIngress:
    """Treat voice as an input channel only; governance remains identical to typed instructions."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        submit_instruction: SubmitDirectInstruction,
    ) -> None:
        self._capabilities = capabilities
        self._submit = submit_instruction

    def __call__(self, item: VoiceTranscript) -> WorkItem:
        self._capabilities.require_enabled(Capability.VOICE)
        transcript = item.transcript.strip()
        if not transcript:
            raise ValueError("Voice transcript cannot be empty.")
        return self._submit(
            SubmitDirectInstructionRequest(
                staff_id=item.staff_id,
                instruction=transcript,
                actor_id=item.actor_id,
            )
        )
