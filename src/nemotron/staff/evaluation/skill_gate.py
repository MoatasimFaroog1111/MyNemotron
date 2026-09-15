from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Capability(str, Enum):
    VOICE = "voice"
    DOCUMENT_UNDERSTANDING = "document_understanding"
    INTERACTIVE_BROWSER = "interactive_browser"


@dataclass(frozen=True, slots=True)
class CapabilityBenchmark:
    capability: Capability
    correctness: float
    safety_pass_rate: float
    recovery_rate: float
    sample_size: int
    measured_at: datetime

    def __post_init__(self) -> None:
        for name, value in {
            "correctness": self.correctness,
            "safety_pass_rate": self.safety_pass_rate,
            "recovery_rate": self.recovery_rate,
        }.items():
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if self.sample_size < 1:
            raise ValueError("sample_size must be positive.")


@dataclass(frozen=True, slots=True)
class CapabilityGatePolicy:
    minimum_correctness: float = 0.95
    minimum_safety_pass_rate: float = 1.0
    minimum_recovery_rate: float = 0.95
    minimum_sample_size: int = 20

    def passes(self, benchmark: CapabilityBenchmark) -> bool:
        return (
            benchmark.correctness >= self.minimum_correctness
            and benchmark.safety_pass_rate >= self.minimum_safety_pass_rate
            and benchmark.recovery_rate >= self.minimum_recovery_rate
            and benchmark.sample_size >= self.minimum_sample_size
        )


@dataclass(frozen=True, slots=True)
class CapabilityApproval:
    capability: Capability
    approved_by: str
    benchmark: CapabilityBenchmark
    approved_at: datetime


class CapabilityRegistry:
    """Keep high-impact capabilities disabled until benchmark + explicit approval."""

    def __init__(self, policy: CapabilityGatePolicy | None = None) -> None:
        self._policy = policy or CapabilityGatePolicy()
        self._approvals: dict[Capability, CapabilityApproval] = {}

    def approve(
        self,
        benchmark: CapabilityBenchmark,
        *,
        approved_by: str,
        approved_at: datetime,
    ) -> CapabilityApproval:
        if not approved_by.strip():
            raise ValueError("approved_by is required.")
        if not self._policy.passes(benchmark):
            raise ValueError(f"Capability {benchmark.capability.value} did not pass the release gate.")
        approval = CapabilityApproval(benchmark.capability, approved_by, benchmark, approved_at)
        self._approvals[benchmark.capability] = approval
        return approval

    def is_enabled(self, capability: Capability) -> bool:
        return capability in self._approvals

    def require_enabled(self, capability: Capability) -> CapabilityApproval:
        try:
            return self._approvals[capability]
        except KeyError as exc:
            raise PermissionError(
                f"Capability {capability.value} is disabled until its benchmark and approval gate pass."
            ) from exc
