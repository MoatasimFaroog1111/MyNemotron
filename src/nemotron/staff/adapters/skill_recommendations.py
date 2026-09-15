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
    """Recommend training targets from explainable bilingual metadata overlap."""

    _FINANCE_MARKERS = {
        "accounting",
        "accountant",
        "bank",
        "reconciliation",
        "odoo",
        "invoice",
        "vat",
        "ledger",
        "finance",
        "محاسبة",
        "محاسب",
        "مالية",
        "بنك",
        "تسويات",
        "فاتورة",
        "ضريبة",
    }
    _ENGINEERING_MARKERS = {
        "python",
        "react",
        "frontend",
        "backend",
        "developer",
        "software",
        "api",
        "integration",
        "engineering",
        "تطوير",
        "مطور",
        "برمجة",
        "تكامل",
        "هندسة",
    }

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
            profile_tokens = self._profile_tokens(profile)
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
            rationale=f"Deterministic bilingual metadata overlap score={best}.",
        )

    def _skill_tokens(self, skill: SkillDefinition) -> set[str]:
        tokens = self._tokens(" ".join((skill.skill_id, skill.name, skill.description)))
        return self._expand_taxonomy(tokens)

    def _profile_tokens(self, profile: StaffTrainingProfile) -> set[str]:
        tokens = self._tokens(
            " ".join(
                (
                    profile.staff_id,
                    profile.department_id,
                    profile.department_name,
                    profile.role_name,
                    profile.job_title,
                )
            )
        )
        if profile.department_id.casefold() == "finance":
            tokens.update({"finance", "accounting", "accountant", "reconciliation", "مالية", "محاسبة", "محاسب"})
        elif profile.department_id.casefold() in {"engineering", "ai", "infrastructure"}:
            tokens.update({"engineering", "developer", "software", "integration", "تطوير", "مطور", "تكامل"})
        return self._expand_taxonomy(tokens)

    def _expand_taxonomy(self, tokens: set[str]) -> set[str]:
        expanded = set(tokens)
        if tokens & self._FINANCE_MARKERS:
            expanded.update({"finance", "accounting", "accountant", "reconciliation", "مالية", "محاسبة", "محاسب"})
        if tokens & self._ENGINEERING_MARKERS:
            expanded.update({"engineering", "developer", "software", "integration", "تطوير", "مطور", "تكامل"})
        return expanded

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE) if len(token) > 2}
