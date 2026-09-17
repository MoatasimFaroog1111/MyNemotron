from __future__ import annotations

import copy
import re

from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    StaffCaseOutcome,
    StaffEvaluationCase,
)


class DeterministicContractStaffEvaluationRunner:
    """Exercise evaluation contracts without model, network, tool, or production calls.

    This adapter does not estimate model quality. It materializes only observables
    explicitly declared by the test fixture so scorer/application wiring can be
    validated deterministically. Contract-mode readiness remains fail-closed in
    the domain policy.
    """

    def run(self, case: StaffEvaluationCase) -> StaffCaseOutcome:
        self._validate_case(case)

        blocked = self._blocked(case)
        final_state = self._final_state(case, blocked=blocked)
        decision_text = self._decision_text(case)
        structured_output = self._structured_output(case)
        recovery = case.category is EvaluationCategory.RECOVERY and case.inject_failure

        return StaffCaseOutcome(
            final_task_state=final_state,
            decision_text=decision_text,
            structured_output=structured_output,
            evidence_references=(f"contract-fixture:{case.case_id}",),
            blocked=blocked,
            attempts=2 if recovery else 1,
            recovered_after_failure=recovery,
            end_to_end_latency_ms=0.0,
            provider_latency_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cost_usd=None,
            audit_event_types=("contract.synthetic_outcome",),
            execution_references=(),
        )

    @staticmethod
    def _validate_case(case: StaffEvaluationCase) -> None:
        if not case.rubric.has_observable_checks() and "structured_output" not in case.expected:
            raise ValueError("contract evaluation case has no explicit observable contract")
        if case.category is EvaluationCategory.RECOVERY and not case.inject_failure:
            raise ValueError("contract recovery case must declare controlled failure injection")
        if case.inject_failure and case.failure_kind not in {
            "reasoner_timeout_once",
            "repository_transient_once",
        }:
            raise ValueError("unsupported contract recovery failure kind")

    @staticmethod
    def _blocked(case: StaffEvaluationCase) -> bool:
        if case.rubric.must_block is not None:
            return case.rubric.must_block
        return bool(case.rubric.expected_task_states) and case.rubric.expected_task_states[0] == "blocked"

    @staticmethod
    def _final_state(case: StaffEvaluationCase, *, blocked: bool) -> str:
        if case.rubric.expected_task_states:
            return case.rubric.expected_task_states[0]
        return "blocked" if blocked else "ready_for_execution"

    @staticmethod
    def _structured_output(case: StaffEvaluationCase) -> dict | None:
        value = case.expected.get("structured_output")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("contract structured_output fixture must be an object")
        return copy.deepcopy(value)

    def _decision_text(self, case: StaffEvaluationCase) -> str:
        explicit = case.expected.get("contract_decision_text")
        if explicit is not None:
            if not isinstance(explicit, str) or not explicit.strip():
                raise ValueError("contract_decision_text must be a non-empty string")
            text = explicit.strip()
        else:
            text = self._language_fixture(case)
            if case.rubric.required_substrings:
                text = f"{text} {' | '.join(case.rubric.required_substrings)}"

        folded = text.casefold()
        for forbidden in case.rubric.forbidden_substrings:
            if forbidden.casefold() in folded:
                raise ValueError("contract fixture conflicts with forbidden substring")

        for pattern in case.rubric.required_regex:
            try:
                matched = re.search(pattern, text) is not None
            except re.error as exc:
                raise ValueError("contract fixture contains invalid required regex") from exc
            if not matched:
                if explicit is None:
                    raise ValueError(
                        "required_regex needs explicit expected.contract_decision_text in contract mode"
                    )
                raise ValueError("contract_decision_text does not satisfy required regex")
        return text

    @staticmethod
    def _language_fixture(case: StaffEvaluationCase) -> str:
        language = (case.rubric.expected_language or case.language).strip().lower()
        if language.startswith("ar"):
            return (
                "هذه استجابة عربية تجريبية واضحة ومختصرة للتحقق من عقد التقييم فقط "
                "دون استنتاج جودة النموذج أو تنفيذ أي إجراء خارجي."
            )
        if language.startswith("en"):
            return (
                "This is a concise deterministic contract fixture response used only "
                "to validate evaluation wiring without judging model quality."
            )
        raise ValueError(f"unsupported contract fixture language: {language}")
