from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nemotron.staff.adapters.sqlite_knowledge import SQLiteKnowledgeRepository
from nemotron.staff.application.institutional_memory import DecideKnowledgeCorrection
from nemotron.staff.domain import (
    Department,
    Organization,
    Permission,
    PermissionDenied,
    RiskLevel,
    Role,
    StaffMember,
    StaffPlacement,
)
from nemotron.staff.domain.knowledge import (
    KnowledgeCorrection,
    KnowledgeRecord,
    KnowledgeScope,
    KnowledgeSourceKind,
)


NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)


class StaffRepo:
    def __init__(self, *members: StaffMember) -> None:
        self.items = {member.staff_id: member for member in members}

    def get(self, staff_id: str) -> StaffMember:
        return self.items[staff_id]


class OrgRepo:
    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    def get(self, organization_id: str) -> Organization:
        if organization_id != self.organization.organization_id:
            raise LookupError(organization_id)
        return self.organization


class Ids:
    def new_id(self) -> str:
        return "replacement-1"


class Clock:
    def now(self) -> datetime:
        return NOW


class Audit:
    def append(self, event) -> None:
        pass


def test_correction_approver_must_belong_to_target_organization(tmp_path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "knowledge.sqlite3")
    target = KnowledgeRecord(
        "knowledge-1",
        "org-1",
        "writer",
        KnowledgeScope.ORGANIZATION,
        "Old fact",
        KnowledgeSourceKind.DOCUMENT,
        "policy:v1",
        NOW - timedelta(days=2),
        NOW - timedelta(days=1),
    )
    repository.save_record(target)
    repository.save_correction(
        KnowledgeCorrection(
            "correction-1",
            "org-1",
            target.knowledge_id,
            "New fact",
            KnowledgeSourceKind.DOCUMENT,
            "policy:v2",
            NOW - timedelta(hours=2),
            "writer",
            NOW - timedelta(hours=1),
        )
    )

    approver = StaffMember(
        "outsider",
        "Outsider Approver",
        Role(
            "approver-role",
            "Approver",
            (Permission("knowledge.approve", "institutional-memory", RiskLevel.HIGH),),
            approval_limit=RiskLevel.HIGH,
        ),
    )
    organization = Organization(
        "org-1",
        "Org One",
        departments=(Department("finance", "Finance"),),
        placements=(StaffPlacement("writer", "finance", "Writer"),),
    )

    decide = DecideKnowledgeCorrection(
        repository,
        StaffRepo(approver),
        OrgRepo(organization),
        Ids(),
        Clock(),
        Audit(),
    )

    with pytest.raises(PermissionDenied, match="correction organization"):
        decide("correction-1", "outsider", approved=True, rationale="should not be allowed")
