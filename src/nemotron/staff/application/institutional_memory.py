from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nemotron.staff.domain import PermissionDenied, RiskLevel
from nemotron.staff.domain.knowledge import (
    CorrectionStatus,
    KnowledgeCorrection,
    KnowledgeRecord,
    KnowledgeScope,
    KnowledgeSourceKind,
)

from .ports import AuditPort, ClockPort, GovernanceAuditEvent, IdGeneratorPort, OrganizationRepository, StaffRepository


class KnowledgeRepository(Protocol):
    def save_record(self, record: KnowledgeRecord) -> None:
        """Persist an immutable knowledge record."""

    def get_record(self, knowledge_id: str) -> KnowledgeRecord:
        """Return a knowledge record or raise LookupError."""

    def search_active(self, organization_id: str, query: str) -> tuple[KnowledgeRecord, ...]:
        """Search active knowledge inside one organization."""

    def save_correction(self, correction: KnowledgeCorrection) -> None:
        """Persist a correction proposal/decision."""

    def get_correction(self, correction_id: str) -> KnowledgeCorrection:
        """Return a correction or raise LookupError."""

    def apply_approved_correction(
        self,
        correction: KnowledgeCorrection,
        *,
        target: KnowledgeRecord,
        replacement: KnowledgeRecord,
    ) -> None:
        """Atomically supersede target, insert replacement, and persist approved correction."""


class RecordKnowledge:
    def __init__(
        self,
        repository: KnowledgeRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._repository = repository
        self._staff = staff
        self._organizations = organizations
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        organization_id: str,
        actor_id: str,
        *,
        scope: KnowledgeScope,
        content: str,
        source_kind: KnowledgeSourceKind,
        source_reference: str,
        source_observed_at: datetime,
    ) -> KnowledgeRecord:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("knowledge.write", "institutional-memory", RiskLevel.MEDIUM)
        organization = self._organizations.get(organization_id)
        placement = organization.placement_for(actor_id)
        now = self._clock.now()
        record = KnowledgeRecord(
            knowledge_id=self._ids.new_id(),
            organization_id=organization_id,
            owner_staff_id=actor_id,
            scope=scope,
            content=content,
            source_kind=source_kind,
            source_reference=source_reference,
            source_observed_at=source_observed_at,
            recorded_at=now,
            department_id=placement.department_id if scope is KnowledgeScope.DEPARTMENT else None,
        )
        self._repository.save_record(record)
        self._audit.append(
            GovernanceAuditEvent(
                "knowledge.recorded",
                "knowledge",
                record.knowledge_id,
                actor_id,
                now,
                f"{scope.value}:{source_kind.value}:{source_reference}",
            )
        )
        return record


class SearchKnowledge:
    def __init__(
        self,
        repository: KnowledgeRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
    ) -> None:
        self._repository = repository
        self._staff = staff
        self._organizations = organizations

    def __call__(self, organization_id: str, requester_id: str, *, query: str) -> tuple[KnowledgeRecord, ...]:
        requester = self._staff.get(requester_id)
        requester.assert_allowed("knowledge.read", "institutional-memory", RiskLevel.LOW)
        organization = self._organizations.get(organization_id)
        department_id = organization.placement_for(requester_id).department_id
        return tuple(
            record
            for record in self._repository.search_active(organization_id, query)
            if self._visible(record, requester_id=requester_id, department_id=department_id)
        )

    @staticmethod
    def _visible(record: KnowledgeRecord, *, requester_id: str, department_id: str) -> bool:
        if record.scope is KnowledgeScope.ORGANIZATION:
            return True
        if record.scope is KnowledgeScope.PRIVATE:
            return record.owner_staff_id == requester_id
        return record.department_id == department_id


class ProposeKnowledgeCorrection:
    def __init__(
        self,
        repository: KnowledgeRepository,
        staff: StaffRepository,
        organizations: OrganizationRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._repository = repository
        self._staff = staff
        self._organizations = organizations
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        organization_id: str,
        actor_id: str,
        *,
        target_knowledge_id: str,
        proposed_content: str,
        source_kind: KnowledgeSourceKind,
        source_reference: str,
        source_observed_at: datetime,
    ) -> KnowledgeCorrection:
        actor = self._staff.get(actor_id)
        actor.assert_allowed("knowledge.correct", "institutional-memory", RiskLevel.MEDIUM)
        target = self._repository.get_record(target_knowledge_id)
        if target.organization_id != organization_id or not target.is_active:
            raise PermissionDenied("Correction target is not an active record in this organization.")
        organization = self._organizations.get(organization_id)
        department_id = organization.placement_for(actor_id).department_id
        if not SearchKnowledge._visible(target, requester_id=actor_id, department_id=department_id):
            raise PermissionDenied("Correction target is not visible to this actor.")
        now = self._clock.now()
        correction = KnowledgeCorrection(
            correction_id=self._ids.new_id(),
            organization_id=organization_id,
            target_knowledge_id=target_knowledge_id,
            proposed_content=proposed_content,
            source_kind=source_kind,
            source_reference=source_reference,
            source_observed_at=source_observed_at,
            proposed_by=actor_id,
            proposed_at=now,
        )
        self._repository.save_correction(correction)
        self._audit.append(
            GovernanceAuditEvent(
                "knowledge.correction_proposed",
                "knowledge_correction",
                correction.correction_id,
                actor_id,
                now,
                target_knowledge_id,
            )
        )
        return correction


class DecideKnowledgeCorrection:
    def __init__(
        self,
        repository: KnowledgeRepository,
        staff: StaffRepository,
        ids: IdGeneratorPort,
        clock: ClockPort,
        audit: AuditPort,
    ) -> None:
        self._repository = repository
        self._staff = staff
        self._ids = ids
        self._clock = clock
        self._audit = audit

    def __call__(
        self,
        correction_id: str,
        approver_id: str,
        *,
        approved: bool,
        rationale: str,
    ) -> KnowledgeCorrection:
        correction = self._repository.get_correction(correction_id)
        approver = self._staff.get(approver_id)
        approver.assert_allowed("knowledge.approve", "institutional-memory", RiskLevel.HIGH)
        now = self._clock.now()
        decided = correction.decide(
            approver_id=approver_id,
            approved=approved,
            at=now,
            rationale=rationale,
        )
        target = self._repository.get_record(correction.target_knowledge_id)
        if approved:
            if not target.is_active:
                raise PermissionDenied("Correction target was superseded before approval.")
            replacement = KnowledgeRecord(
                knowledge_id=self._ids.new_id(),
                organization_id=target.organization_id,
                owner_staff_id=correction.proposed_by,
                scope=target.scope,
                content=correction.proposed_content,
                source_kind=correction.source_kind,
                source_reference=correction.source_reference,
                source_observed_at=correction.source_observed_at,
                recorded_at=now,
                department_id=target.department_id,
            )
            self._repository.apply_approved_correction(decided, target=target, replacement=replacement)
        else:
            self._repository.save_correction(decided)

        self._audit.append(
            GovernanceAuditEvent(
                "knowledge.correction_approved" if approved else "knowledge.correction_rejected",
                "knowledge_correction",
                correction_id,
                approver_id,
                now,
                rationale,
            )
        )
        return decided
