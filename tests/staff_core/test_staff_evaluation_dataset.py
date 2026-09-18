from __future__ import annotations

from collections import Counter
from pathlib import Path

from nemotron.staff.adapters.jsonl_staff_evaluation import JsonlStaffEvaluationCaseRepository
from nemotron.staff.domain.staff_evaluation import EvaluationCategory


PRIMARY = {
    "staff-operations-monitor",
    "staff-data-analyst",
    "staff-systems-developer",
    "staff-project-manager",
    "staff-ux-specialist",
    "staff-integration-engineer",
    "staff-financial-accountant",
    "staff-cybersecurity",
    "staff-content-manager",
    "staff-advanced-analytics",
    "staff-ai-specialist",
    "staff-infrastructure-manager",
    "staff-network-manager",
    "staff-bank-reconciliation",
    "staff-financial-reporting",
    "staff-customer-support",
}

EXPECTED_CATEGORY_COUNTS = {
    EvaluationCategory.CORRECTNESS: 12,
    EvaluationCategory.SAFETY: 4,
    EvaluationCategory.RECOVERY: 2,
    EvaluationCategory.LANGUAGE_CONTRACT: 2,
}


def repository() -> JsonlStaffEvaluationCaseRepository:
    return JsonlStaffEvaluationCaseRepository(Path("evals"))


def test_manifest_contains_exactly_the_16_primary_office_staff() -> None:
    manifest = repository().load_office_manifest()
    assert set(manifest.staff_ids) == PRIMARY
    assert "staff-sherman-trainer" not in manifest.staff_ids
    assert manifest.suite_id == "gold-v1"


def test_every_primary_staff_suite_has_exactly_20_cases_with_12_4_2_2_distribution() -> None:
    repo = repository()
    all_ids: list[str] = []

    for staff_id in sorted(PRIMARY):
        identity, cases = repo.load_suite(staff_id, "gold-v1")
        assert identity.suite_id == "gold-v1"
        assert len(identity.dataset_digest) == 64
        assert len(cases) == 20
        assert Counter(case.category for case in cases) == EXPECTED_CATEGORY_COUNTS
        assert all(case.staff_id == staff_id for case in cases)
        assert all(case.case_id.startswith(f"{staff_id}:") for case in cases)
        assert all(case.instruction.strip() for case in cases)
        assert all(case.expected or case.rubric.has_observable_checks() for case in cases)
        all_ids.extend(case.case_id for case in cases)

    assert len(all_ids) == 320
    assert len(all_ids) == len(set(all_ids))


def test_safety_cases_have_explicit_observable_prohibitions_or_requirements() -> None:
    repo = repository()
    for staff_id in PRIMARY:
        _, cases = repo.load_suite(staff_id, "gold-v1")
        safety = [case for case in cases if case.category is EvaluationCategory.SAFETY]
        assert len(safety) == 4
        for case in safety:
            rubric = case.rubric
            assert (
                rubric.required_substrings
                or rubric.forbidden_substrings
                or rubric.required_regex
                or rubric.must_block is not None
                or rubric.expected_task_states
            )


def test_recovery_cases_declare_controlled_failure_kinds() -> None:
    repo = repository()
    expected = {"reasoner_timeout_once", "repository_transient_once"}
    for staff_id in PRIMARY:
        _, cases = repo.load_suite(staff_id, "gold-v1")
        recovery = [case for case in cases if case.category is EvaluationCategory.RECOVERY]
        assert {case.failure_kind for case in recovery} == expected
        assert all(case.inject_failure for case in recovery)


def test_dataset_digest_is_deterministic_across_repeated_loads() -> None:
    repo = repository()
    for staff_id in PRIMARY:
        left, left_cases = repo.load_suite(staff_id, "gold-v1")
        right, right_cases = repo.load_suite(staff_id, "gold-v1")
        assert left.dataset_digest == right.dataset_digest
        assert left_cases == right_cases
