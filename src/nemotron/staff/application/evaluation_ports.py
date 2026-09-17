from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nemotron.staff.domain.staff_evaluation import (
    EvaluationSuiteIdentity,
    OfficeEvaluationReport,
    StaffCaseOutcome,
    StaffEvaluationCase,
    StaffEvaluationReport,
)


class StaffEvaluationRunner(Protocol):
    def run(self, case: StaffEvaluationCase) -> StaffCaseOutcome:
        """Run one staff evaluation case and return observable governed outcomes."""


class StaffEvaluationCaseRepository(Protocol):
    def load_suite(
        self,
        staff_id: str,
        suite_id: str,
    ) -> tuple[EvaluationSuiteIdentity, tuple[StaffEvaluationCase, ...]]:
        """Load one immutable staff evaluation suite and its stable identity."""


class StaffEvaluationReportRepository(Protocol):
    def append_staff(self, report: StaffEvaluationReport) -> None:
        """Append one immutable staff evaluation report."""

    def get_staff(self, report_id: str) -> StaffEvaluationReport | None:
        """Return one staff report by immutable id, if present."""

    def list_staff(
        self,
        staff_id: str | None = None,
        *,
        limit: int = 100,
    ) -> tuple[StaffEvaluationReport, ...]:
        """List immutable staff reports newest first."""

    def append_office(self, report: OfficeEvaluationReport) -> None:
        """Append one immutable office evaluation report."""

    def get_office(self, run_id: str) -> OfficeEvaluationReport | None:
        """Return one office report by immutable run id, if present."""

    def list_completed_staff(self, run_id: str) -> tuple[str, ...]:
        """Return staff ids already persisted for a resumable office run."""


class EvaluationClock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware evaluation timestamp."""
