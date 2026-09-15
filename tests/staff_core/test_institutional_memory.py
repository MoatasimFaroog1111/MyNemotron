from datetime import datetime, timedelta, timezone

import pytest

from nemotron.staff.adapters.sqlite_knowledge import SQLiteKnowledgeRepository
from nemotron.staff.application.institutional_memory import (
    DecideKnowledgeCorrection,
    ProposeKnowledgeCorrection,
    RecordKnowledge,
    SearchKnowledge,
)
from nemotron.staff.domain import Department, Organization, Permission, RiskLevel, Role, StaffMember, StaffPlacement
from nemotron.staff.domain.knowledge import KnowledgeScope, KnowledgeSourceKind


UTC = timezone.utc


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new_id(self) -> str:
        self.value += 1
        return f"id-{self.value}"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


class Audit:
    def __init__(self) -> None:
        self.events = []

    def append(self, event) -> None:
        self.events.append(event)


class StaffRepo:
    def __init__(self, members) -> None:
        self.members = {member.staff_id: member for member in members}

    def get(self, staff_id: str):
        return self.members[staff_id]

    def save(self, member) -> None:
        self.members[member.staff_id] = member

    def list_all(self):
        return tuple(self.members.values())


class OrgRepo:
    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    def get(self, organization_id: str):
        assert organization_id == self.organization.organization_id
        return self.organization

    def save(self, organization) -> None:
        self.organization = organization


def _member(staff_id: str, *, approver: bool = False) -> StaffMember:
    permissions = [
        Permission("knowledge.read", "institutional-memory", RiskLevel.LOW),
        Permission("knowledge.write", "institutional-memory", RiskLevel.MEDIUM),
        Permission("knowledge.correct", "institutional-memory", RiskLevel.MEDIUM),
    ]
    if approver:
        permissions.append(Permission("knowledge.approve", "institutional-memory", RiskLevel.HIGH))
    return StaffMember(
        staff_id,
        staff_id,
        Role(
            f"role-{staff_id}",
            staff_id,
            tuple(permissions),
            approval_limit=RiskLevel.HIGH if approver else None,
        ),
    )


def _organization() -> Organization:
    return Organization(
        "org-1",
        "Org",
        departments=(Department("finance", "Finance"), Department("engineering", "Engineering")),
        placements=(
            StaffPlacement("writer", "finance", "Writer"),
            StaffPlacement("finance-reader", "finance", "Reader"),
            StaffPlacement("engineering-reader", "engineering", "Reader"),
            StaffPlacement("approver", "finance", "Approver"),
        ),
    )


def test_search_respects_scope_and_preserves_provenance(tmp_path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    staff = StaffRepo(
        (_member("writer"), _member("finance-reader"), _member("engineering-reader"), _member("approver", approver=True))
    )
    organizations = OrgRepo(_organization())
    ids = Ids()
    clock = Clock()
    audit = Audit()
    record = RecordKnowledge(repository, staff, organizations, ids, clock, audit)

    document_date = datetime(2026, 9, 10, tzinfo=UTC)
    private = record(
        "org-1",
        "writer",
        scope=KnowledgeScope.PRIVATE,
        content="Vendor Alpha uses account 400020",
        source_kind=KnowledgeSourceKind.DOCUMENT,
        source_reference="drive:vendor-alpha-policy.pdf#p2",
        source_observed_at=document_date,
    )
    department = record(
        "org-1",
        "writer",
        scope=KnowledgeScope.DEPARTMENT,
        content="Bank reconciliation tolerance is three days",
        source_kind=KnowledgeSourceKind.LOG,
        source_reference="audit-log:reconciliation-policy:v3",
        source_observed_at=document_date,
    )
    record(
        "org-1",
        "writer",
        scope=KnowledgeScope.ORGANIZATION,
        content="Use SAR as the base reporting currency",
        source_kind=KnowledgeSourceKind.DOCUMENT,
        source_reference="drive:accounting-policy.pdf#currency",
        source_observed_at=document_date,
    )

    search = SearchKnowledge(repository, staff, organizations)
    assert [item.knowledge_id for item in search("org-1", "writer", query="Vendor Alpha")] == [private.knowledge_id]
    assert [item.knowledge_id for item in search("org-1", "finance-reader", query="reconciliation tolerance")] == [department.knowledge_id]
    assert search("org-1", "engineering-reader", query="reconciliation tolerance") == ()

    visible_org = search("org-1", "engineering-reader", query="reporting currency")
    assert len(visible_org) == 1
    assert visible_org[0].source_reference == "drive:accounting-policy.pdf#currency"
    assert visible_org[0].source_observed_at == document_date


def test_approved_correction_supersedes_old_fact_atomically(tmp_path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    staff = StaffRepo((_member("writer"), _member("finance-reader"), _member("engineering-reader"), _member("approver", approver=True)))
    organizations = OrgRepo(_organization())
    ids = Ids()
    clock = Clock()
    audit = Audit()
    record = RecordKnowledge(repository, staff, organizations, ids, clock, audit)
    source_date = datetime(2026, 9, 10, tzinfo=UTC)
    original = record(
        "org-1",
        "writer",
        scope=KnowledgeScope.DEPARTMENT,
        content="Tolerance is three days",
        source_kind=KnowledgeSourceKind.DOCUMENT,
        source_reference="policy:v1",
        source_observed_at=source_date,
    )

    propose = ProposeKnowledgeCorrection(repository, staff, organizations, ids, clock, audit)
    correction = propose(
        "org-1",
        "writer",
        target_knowledge_id=original.knowledge_id,
        proposed_content="Tolerance is one day",
        source_kind=KnowledgeSourceKind.DOCUMENT,
        source_reference="policy:v2",
        source_observed_at=datetime(2026, 9, 14, tzinfo=UTC),
    )

    decide = DecideKnowledgeCorrection(repository, staff, ids, clock, audit)
    with pytest.raises(Exception):
        decide(correction.correction_id, "writer", approved=True, rationale="self approval")

    approved = decide(correction.correction_id, "approver", approved=True, rationale="Policy v2 approved")
    assert approved.status.value == "approved"

    old = repository.get_record(original.knowledge_id)
    assert old.superseded_by is not None

    search = SearchKnowledge(repository, staff, organizations)
    assert search("org-1", "finance-reader", query="three days") == ()
    replacement = search("org-1", "finance-reader", query="one day")
    assert len(replacement) == 1
    assert replacement[0].source_reference == "policy:v2"
