from __future__ import annotations

import re
from dataclasses import dataclass

from nemotron.staff.domain.skills import SkillDefinition, TrainingRecommendation


@dataclass(frozen=True, slots=True)
class StaffTrainingProfile:
    staff_id: str
    department_id: str
    department_name: str
    role_name: str
    job_title: str


class DeterministicSkillRecommendationEngine:
    """Recommend training targets from explainable metadata overlap, never model guesswork."""

    def __init__(self, *, min_score: int = 2) -> None:
        if min_score < 1:
            raise ValueError("min_score must be at least 1.")
        self.min_score = min_score

    def recommend(
        self,
        skill: SkillDefinition,
        profiles: tuple[StaffTrainingProfile, ...],
    ) -> TrainingRecommendation:
        skill_tokens = self._skill_tokens(skill)
        scored: list[tuple[int, str]] = []
        for profile in profiles:
            profile_tokens = self._tokens(
                " ".join(
                    (
                        profile.department_id,
                        profile.department_name,
                        profile.role_name,
                        profile.job_title,
                    )
                )
            )
            score = len(skill_tokens & profile_tokens)
            if score >= self.min_score:
                scored.append((score, profile.staff_id))
        if not scored:
            return TrainingRecommendation(
                skill_id=skill.skill_id,
                staff_ids=(),
                confidence=0.0,
                rationale="Low metadata overlap; manual employee selection is required.",
            )
        best = max(score for score, _ in scored)
        selected = tuple(sorted(staff_id for score, staff_id in scored if score == best))
        confidence = min(1.0, best / max(1, len(skill_tokens)))
        return TrainingRecommendation(
            skill_id=skill.skill_id,
            staff_ids=selected,
            confidence=confidence,
            rationale=f"Deterministic metadata overlap score={best}.",
        )

    def _skill_tokens(self, skill: SkillDefinition) -> set[str]:
        text = " ".join((skill.skill_id, skill.name, skill.description))
        tokens = self._tokens(text)
        finance_markers = {"accounting", "accountant", "bank", "reconciliation", "odoo", "invoice", "vat", "ledger"}
        engineering_markers = {"python", "react", "frontend", "backend", "developer", "software", "api", "integration"}
        if tokens & finance_markers:
            tokens.update({"finance", "accounting", "accountant", "reconciliation"})
        if tokens & engineering_markers:
            tokens.update({"engineering", "developer", "software", "integration"})
        return tokens

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE) if len(token) > 2}
