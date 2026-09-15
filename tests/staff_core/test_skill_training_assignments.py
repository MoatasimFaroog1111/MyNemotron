from __future__ import annotations

from datetime import datetime, timezone

from nemotron.staff.adapters.skill_recommendations import (
    DeterministicSkillRecommendationEngine,
    StaffTrainingProfile,
)
from nemotron.staff.adapters.sqlite_skills import SQLiteSkillRegistry
from nemotron.staff.application.skill_training import AssignSkillTraining, ResolveAssignedSkills
from nemotron.staff.domain.skills import SkillDefinition, TrainingStatus


def _skill() -> SkillDefinition:
    return SkillDefinition(
        skill_id="odoo-accounting-review",
        name="Odoo Accounting Review",
        description="Review Odoo accounting entries and bank reconciliation evidence.",
        instructions="Use evidence first and keep financial writes draft-only.",
        source_path="skills/accounting/SKILL.md",
    )


def test_skill_definition_is_inert_domain_content() -> None:
    skill = _skill()
    assert skill.skill_id == "odoo-accounting-review"
    assert TrainingStatus.ACTIVE.value == "active"


def test_registry_is_digest_idempotent_and_resolves_only_active_assignments(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = SQLiteSkillRegistry(tmp_path / "skills.sqlite3")
    imported_at = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)

    first = registry.import_version(
        package_sha256="package-digest",
        source_filename="skills.zip",
        skill=_skill(),
        imported_at=imported_at,
        imported_by="staff-sherman-trainer",
    )
    second = registry.import_version(
        package_sha256="package-digest",
        source_filename="skills.zip",
        skill=_skill(),
        imported_at=imported_at,
        imported_by="staff-sherman-trainer",
    )

    assert first.version_id == second.version_id
    assert len(registry.list_versions()) == 1

    assign = AssignSkillTraining(registry)
    assigned = assign(
        first.version_id,
        ("staff-financial-accountant",),
        assigned_by="ui-operator",
        assigned_at=imported_at,
    )
    repeated = assign(
        first.version_id,
        ("staff-financial-accountant",),
        assigned_by="ui-operator",
        assigned_at=imported_at,
    )

    assert assigned == repeated
    assert ResolveAssignedSkills(registry)("staff-financial-accountant")[0].skill.skill_id == "odoo-accounting-review"

    registry.set_version_status(first.version_id, TrainingStatus.INACTIVE)
    assert ResolveAssignedSkills(registry)("staff-financial-accountant") == ()


def test_recommendations_are_deterministic_and_role_relevant() -> None:
    engine = DeterministicSkillRecommendationEngine(min_score=2)
    profiles = (
        StaffTrainingProfile(
            staff_id="staff-financial-accountant",
            department_id="finance",
            department_name="المالية finance",
            role_name="المحاسبة المالية accounting",
            job_title="محاسب مالي accountant",
        ),
        StaffTrainingProfile(
            staff_id="staff-systems-developer",
            department_id="engineering",
            department_name="الهندسة engineering",
            role_name="تطوير الأنظمة developer",
            job_title="مطور أنظمة software developer",
        ),
    )

    first = engine.recommend(_skill(), profiles)
    second = engine.recommend(_skill(), tuple(reversed(profiles)))

    assert first.staff_ids == ("staff-financial-accountant",)
    assert first == second
    assert first.confidence > 0


def test_low_confidence_recommendation_requires_manual_selection() -> None:
    engine = DeterministicSkillRecommendationEngine(min_score=2)
    generic = SkillDefinition(
        skill_id="generic",
        name="General Procedure",
        description="A neutral process with no role metadata.",
        instructions="Follow the procedure.",
        source_path="SKILL.md",
    )
    profiles = (
        StaffTrainingProfile("staff-financial-accountant", "finance", "finance", "accounting", "accountant"),
    )

    recommendation = engine.recommend(generic, profiles)

    assert recommendation.staff_ids == ()
    assert recommendation.confidence == 0
