from __future__ import annotations

from nemotron.staff.domain.skills import SkillDefinition, TrainingStatus


def test_skill_definition_is_inert_domain_content() -> None:
    skill = SkillDefinition(
        skill_id="accounting-review",
        name="Accounting Review",
        description="Review accounting evidence.",
        instructions="Use evidence first.",
        source_path="skills/accounting/SKILL.md",
    )

    assert skill.skill_id == "accounting-review"
    assert skill.name == "Accounting Review"
    assert skill.instructions == "Use evidence first."
    assert TrainingStatus.ACTIVE.value == "active"
