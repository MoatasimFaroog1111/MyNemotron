from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nemotron.staff.domain.staff_evaluation import (
    EvaluationCategory,
    EvaluationSuiteIdentity,
    StaffEvaluationCase,
    StaffEvaluationRubric,
)


@dataclass(frozen=True, slots=True)
class OfficeEvaluationManifest:
    suite_id: str
    version: int
    staff_ids: tuple[str, ...]
    staff_files: dict[str, str]
    shared_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.suite_id.strip():
            raise ValueError("evaluation manifest suite_id is required")
        if self.version < 1:
            raise ValueError("evaluation manifest version must be positive")
        if not self.staff_ids:
            raise ValueError("evaluation manifest must contain staff")
        if len(self.staff_ids) != len(set(self.staff_ids)):
            raise ValueError("evaluation manifest contains duplicate staff ids")
        if set(self.staff_files) != set(self.staff_ids):
            raise ValueError("evaluation manifest staff files must exactly match staff ids")
        if any(not value.strip() for value in self.shared_files):
            raise ValueError("evaluation manifest shared file paths cannot be blank")


class JsonlStaffEvaluationCaseRepository:
    """Load immutable, versioned staff Gold suites from JSONL files."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def load_office_manifest(self) -> OfficeEvaluationManifest:
        path = self._root / "staff" / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"evaluation manifest not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid evaluation manifest JSON: {exc}") from exc

        staff_ids = tuple(str(item) for item in payload.get("staff_ids", ()))
        staff_files = {str(key): str(value) for key, value in dict(payload.get("staff_files", {})).items()}
        shared_files = tuple(str(item) for item in payload.get("shared_files", ()))
        return OfficeEvaluationManifest(
            suite_id=str(payload.get("suite_id", "")),
            version=int(payload.get("version", 0)),
            staff_ids=staff_ids,
            staff_files=staff_files,
            shared_files=shared_files,
        )

    def load_suite(
        self,
        staff_id: str,
        suite_id: str,
    ) -> tuple[EvaluationSuiteIdentity, tuple[StaffEvaluationCase, ...]]:
        manifest = self.load_office_manifest()
        if suite_id != manifest.suite_id:
            raise ValueError(f"unknown evaluation suite: {suite_id}")
        if staff_id not in manifest.staff_ids:
            raise ValueError(f"staff is not in evaluation manifest: {staff_id}")

        role_payloads = self._load_jsonl(self._root / manifest.staff_files[staff_id])
        cases = [self._role_case(payload, staff_id) for payload in role_payloads]
        for relative_path in manifest.shared_files:
            for payload in self._load_jsonl(self._root / relative_path):
                cases.append(self._shared_case(payload, staff_id))

        ordered = tuple(sorted(cases, key=lambda item: item.case_id))
        ids = tuple(item.case_id for item in ordered)
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate evaluation case id in suite for {staff_id}")
        if any(item.staff_id != staff_id for item in ordered):
            raise ValueError(f"evaluation suite contains case for another staff member: {staff_id}")

        canonical = json.dumps(
            [self._canonical_case(item) for item in ordered],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return EvaluationSuiteIdentity(suite_id=manifest.suite_id, dataset_digest=digest), ordered

    @staticmethod
    def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise ValueError(f"evaluation JSONL not found: {path}") from exc
        result: list[dict[str, Any]] = []
        for line_number, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path}:{line_number}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"evaluation JSONL row must be an object: {path}:{line_number}")
            result.append(payload)
        if not result:
            raise ValueError(f"evaluation JSONL is empty: {path}")
        return tuple(result)

    def _role_case(self, payload: dict[str, Any], staff_id: str) -> StaffEvaluationCase:
        if str(payload.get("staff_id", "")) != staff_id:
            raise ValueError(f"role case staff_id mismatch for {staff_id}")
        case = self._build_case(payload)
        if case.category is not EvaluationCategory.CORRECTNESS:
            raise ValueError("role Gold files may contain correctness cases only")
        if not case.case_id.startswith(f"{staff_id}:"):
            raise ValueError("role case id must be staff-qualified")
        return case

    def _shared_case(self, payload: dict[str, Any], staff_id: str) -> StaffEvaluationCase:
        category = EvaluationCategory(str(payload["category"]))
        template_id = str(payload.get("template_id", "")).strip()
        if not template_id:
            raise ValueError("shared evaluation template_id is required")
        expanded = dict(payload)
        expanded["case_id"] = f"{staff_id}:{category.value}:{template_id}"
        expanded["staff_id"] = staff_id
        expanded.pop("template_id", None)
        return self._build_case(expanded)

    @staticmethod
    def _build_case(payload: dict[str, Any]) -> StaffEvaluationCase:
        rubric_payload = dict(payload.get("rubric", {}))
        rubric = StaffEvaluationRubric(
            required_substrings=tuple(str(v) for v in rubric_payload.get("required_substrings", ())),
            forbidden_substrings=tuple(str(v) for v in rubric_payload.get("forbidden_substrings", ())),
            required_regex=tuple(str(v) for v in rubric_payload.get("required_regex", ())),
            must_block=(None if "must_block" not in rubric_payload else bool(rubric_payload["must_block"])),
            expected_language=(
                None if rubric_payload.get("expected_language") is None else str(rubric_payload["expected_language"])
            ),
            expected_task_states=tuple(str(v) for v in rubric_payload.get("expected_task_states", ())),
        )
        return StaffEvaluationCase(
            case_id=str(payload.get("case_id", "")),
            staff_id=str(payload.get("staff_id", "")),
            category=EvaluationCategory(str(payload["category"])),
            language=str(payload.get("language", "ar")),
            instruction=str(payload.get("instruction", "")),
            expected=dict(payload.get("expected", {})),
            rubric=rubric,
            risk_profile=str(payload.get("risk_profile", "low")),
            inject_failure=bool(payload.get("inject_failure", False)),
            tags=tuple(str(v) for v in payload.get("tags", ())),
            failure_kind=(None if payload.get("failure_kind") is None else str(payload["failure_kind"])),
        )

    @staticmethod
    def _canonical_case(case: StaffEvaluationCase) -> dict[str, Any]:
        return {
            "case_id": case.case_id,
            "staff_id": case.staff_id,
            "category": case.category.value,
            "language": case.language,
            "instruction": case.instruction,
            "expected": case.expected,
            "rubric": {
                "required_substrings": list(case.rubric.required_substrings),
                "forbidden_substrings": list(case.rubric.forbidden_substrings),
                "required_regex": list(case.rubric.required_regex),
                "must_block": case.rubric.must_block,
                "expected_language": case.rubric.expected_language,
                "expected_task_states": list(case.rubric.expected_task_states),
            },
            "risk_profile": case.risk_profile,
            "inject_failure": case.inject_failure,
            "failure_kind": case.failure_kind,
            "tags": list(case.tags),
        }
