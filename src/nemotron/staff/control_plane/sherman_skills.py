from __future__ import annotations

from typing import Any

from nemotron.staff.adapters.safe_skill_archives import SafeSkillArchiveInspector
from nemotron.staff.adapters.skill_recommendations import (
    DeterministicSkillRecommendationEngine,
    StaffTrainingProfile,
)
from nemotron.staff.adapters.sqlite_skills import SQLiteSkillRegistry
from nemotron.staff.application.ports import GovernanceAuditEvent
from nemotron.staff.application.skill_training import AssignSkillTraining, ResolveAssignedSkills
from nemotron.staff.domain.skills import SkillTrainingError, SkillVersion, TrainingStatus

from .runtime import ProductionRuntime


class ShermanSkillControlService:
    """Governed control-plane facade for inert skill import and explicit staff assignment."""

    def __init__(self, runtime: ProductionRuntime) -> None:
        self.runtime = runtime
        self.registry = SQLiteSkillRegistry(runtime.config.database_path)
        self.inspector = SafeSkillArchiveInspector()
        self.recommender = DeterministicSkillRecommendationEngine(min_score=2)
        self.assign_training = AssignSkillTraining(self.registry)
        self.resolve_training = ResolveAssignedSkills(self.registry)

    def import_archive(
        self,
        *,
        filename: str,
        payload: bytes,
        actor_id: str = "staff-sherman-trainer",
    ) -> dict[str, Any]:
        clean_filename = filename.strip()
        actor = actor_id.strip() or "staff-sherman-trainer"
        if not clean_filename:
            raise ValueError("filename is required.")
        if not payload:
            raise ValueError("Skill archive payload is required.")
        inspection = self.inspector.inspect(clean_filename, payload)
        if inspection.rejected:
            self.runtime.audit.append(
                GovernanceAuditEvent(
                    "skill.import_rejected",
                    "skill_package",
                    inspection.package_sha256,
                    actor,
                    self.runtime.clock.now(),
                    inspection.reason,
                )
            )
            raise SkillTrainingError(inspection.reason)

        imported: list[SkillVersion] = []
        imported_at = self.runtime.clock.now()
        for skill in inspection.skills:
            version = self.registry.import_version(
                package_sha256=inspection.package_sha256,
                source_filename=clean_filename,
                skill=skill,
                imported_at=imported_at,
                imported_by=actor,
            )
            imported.append(version)
            self.runtime.audit.append(
                GovernanceAuditEvent(
                    "skill.imported",
                    "skill_version",
                    version.version_id,
                    actor,
                    imported_at,
                    (
                        f"skill_id={version.skill.skill_id}; package_sha256={inspection.package_sha256}; "
                        f"content_sha256={version.content_sha256}; source={version.skill.source_path}"
                    ),
                )
            )
        return {
            "package_sha256": inspection.package_sha256,
            "archive_kind": inspection.archive_kind,
            "skills": [self._version_dict(version) for version in imported],
        }

    def list_skills(self) -> list[dict[str, Any]]:
        return [self._version_dict(version) for version in self.registry.list_versions()]

    def recommend(self, version_id: str, *, actor_id: str = "control-plane") -> dict[str, Any]:
        version = self.registry.get_version(version_id)
        recommendation = self.recommender.recommend(version.skill, self._staff_profiles())
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "skill.training_recommended",
                "skill_version",
                version_id,
                actor_id.strip() or "control-plane",
                self.runtime.clock.now(),
                f"targets={','.join(recommendation.staff_ids)}; confidence={recommendation.confidence:.4f}",
            )
        )
        return {
            "version_id": version_id,
            "skill_id": version.skill.skill_id,
            "staff_ids": list(recommendation.staff_ids),
            "confidence": recommendation.confidence,
            "rationale": recommendation.rationale,
            "activated": False,
        }

    def assign(
        self,
        version_id: str,
        *,
        mode: str,
        actor_id: str = "control-plane",
        staff_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        actor = actor_id.strip() or "control-plane"
        normalized_mode = mode.strip().casefold()
        if normalized_mode == "suggested":
            targets = tuple(self.recommend(version_id, actor_id=actor)["staff_ids"])
        elif normalized_mode == "all":
            targets = tuple(
                sorted(
                    member.staff_id
                    for member in self.runtime.staff.list_all()
                    if member.status.value == "active" and member.staff_id != "staff-sherman-trainer"
                )
            )
        elif normalized_mode == "selected":
            targets = tuple(sorted(set(item.strip() for item in staff_ids if item.strip())))
        else:
            raise ValueError("mode must be suggested, all, or selected.")
        if not targets:
            raise ValueError("No training targets were selected or recommended.")

        known = {member.staff_id: member for member in self.runtime.staff.list_all()}
        invalid = [staff_id for staff_id in targets if staff_id not in known or known[staff_id].status.value != "active"]
        if invalid:
            raise ValueError(f"Unknown or inactive training target: {invalid[0]}")

        assigned_at = self.runtime.clock.now()
        assignments = self.assign_training(
            version_id,
            targets,
            assigned_by=actor,
            assigned_at=assigned_at,
        )
        resolved = {
            staff_id
            for staff_id in targets
            if any(version.version_id == version_id for version in self.resolve_training(staff_id))
        }
        if resolved != set(targets):
            raise RuntimeError("Skill assignment read-back verification failed.")
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "skill.training_assigned",
                "skill_version",
                version_id,
                actor,
                assigned_at,
                f"mode={normalized_mode}; targets={','.join(targets)}; assignments={len(assignments)}",
            )
        )
        return {
            "version_id": version_id,
            "staff_ids": list(targets),
            "activated": True,
            "assignment_ids": [item.assignment_id for item in assignments],
        }

    def resolve_for_staff(self, staff_id: str) -> list[dict[str, Any]]:
        self.runtime.staff.get(staff_id)
        return [self._version_dict(version) for version in self.resolve_training(staff_id)]

    def deactivate(self, version_id: str, *, actor_id: str = "control-plane") -> dict[str, Any]:
        version = self.registry.set_version_status(version_id, TrainingStatus.INACTIVE)
        self.runtime.audit.append(
            GovernanceAuditEvent(
                "skill.deactivated",
                "skill_version",
                version_id,
                actor_id.strip() or "control-plane",
                self.runtime.clock.now(),
                "Skill version deactivated; existing assignments no longer resolve.",
            )
        )
        return self._version_dict(version)

    def _staff_profiles(self) -> tuple[StaffTrainingProfile, ...]:
        profiles: list[StaffTrainingProfile] = []
        for member in self.runtime.staff.list_all():
            if member.status.value != "active" or member.staff_id == "staff-sherman-trainer":
                continue
            department_id = ""
            department_name = ""
            job_title = ""
            for organization in self.runtime.organizations.list_all():
                try:
                    placement = organization.placement_for(member.staff_id)
                    department = organization.department_for(member.staff_id)
                except LookupError:
                    continue
                department_id = department.department_id
                department_name = department.name
                job_title = placement.job_title
                break
            profiles.append(
                StaffTrainingProfile(
                    staff_id=member.staff_id,
                    department_id=department_id,
                    department_name=department_name,
                    role_name=member.role.name,
                    job_title=job_title,
                )
            )
        return tuple(profiles)

    @staticmethod
    def _version_dict(version: SkillVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id,
            "skill_id": version.skill.skill_id,
            "name": version.skill.name,
            "description": version.skill.description,
            "source_path": version.skill.source_path,
            "resources": list(version.skill.resources),
            "package_sha256": version.package_sha256,
            "content_sha256": version.content_sha256,
            "status": version.status.value,
            "imported_at": version.imported_at.isoformat(),
            "imported_by": version.imported_by,
        }
