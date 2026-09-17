from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from nemotron.staff.adapters.contract_staff_evaluation import (
    DeterministicContractStaffEvaluationRunner,
)
from nemotron.staff.adapters.governed_staff_evaluation import (
    GovernedStaffEvaluationRunner,
    IsolatedEvaluationRuntimeFactory,
)
from nemotron.staff.adapters.jsonl_staff_evaluation import JsonlStaffEvaluationCaseRepository
from nemotron.staff.adapters.sqlite_control import SQLiteAuditLog, SQLiteControlStore
from nemotron.staff.adapters.sqlite_staff_evaluation import SQLiteStaffEvaluationReportRepository
from nemotron.staff.application.staff_evaluation import RunOfficeEvaluation, RunStaffEvaluation
from nemotron.staff.application.worker_ports import WorkerReasoningPort
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.runtime import UtcClock
from nemotron.staff.domain.staff_evaluation import EvaluationRunMode, OfficeEvaluationReport


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class EvaluationCLIOptions:
    mode: EvaluationRunMode
    eval_root: Path
    output_dir: Path
    run_id: str
    git_sha: str | None
    model_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip() or _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("run_id must use only letters, numbers, '.', '_' or '-'")
        if self.git_sha is not None and not self.git_sha.strip():
            raise ValueError("git_sha cannot be blank")
        if self.model_id is not None and not self.model_id.strip():
            raise ValueError("model_id cannot be blank")


@dataclass(frozen=True, slots=True)
class EvaluationCLIResult:
    run_id: str
    mode: EvaluationRunMode
    status: str
    ready: bool
    sample_size: int
    report_path: Path
    reports_db_path: Path
    exit_code: int


def run_evaluation_command(
    options: EvaluationCLIOptions,
    *,
    config: ControlPlaneConfig | None = None,
    reasoner: WorkerReasoningPort | None = None,
) -> EvaluationCLIResult:
    eval_root = Path(options.eval_root).resolve()
    output_dir = Path(options.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cases = JsonlStaffEvaluationCaseRepository(eval_root)
    manifest = cases.load_office_manifest()
    reports_db_path = output_dir / "staff-evaluation.sqlite3"
    reports = SQLiteStaffEvaluationReportRepository(reports_db_path)
    control_store = SQLiteControlStore(reports_db_path)
    audit = SQLiteAuditLog(control_store)
    clock = UtcClock()

    if options.mode is EvaluationRunMode.CONTRACT:
        runner = DeterministicContractStaffEvaluationRunner()
        model_id = options.model_id or "contract-fixture"
        config_digest = hashlib.sha256(b"staff-evaluation-contract-cli-v1").hexdigest()
    else:
        selected_config = config or ControlPlaneConfig.from_env()
        runtime_root = output_dir / "runtime" / options.run_id
        runner = GovernedStaffEvaluationRunner(
            IsolatedEvaluationRuntimeFactory(
                selected_config,
                reasoner=reasoner,
                evaluation_root=runtime_root,
            )
        )
        model_id = options.model_id or selected_config.nemotron_model
        config_digest = _config_digest(selected_config)

    run_staff = RunStaffEvaluation(
        cases=cases,
        runner=runner,
        reports=reports,
        clock=clock,
        audit=audit,
    )
    run_office = RunOfficeEvaluation(
        run_staff=run_staff,
        reports=reports,
        clock=clock,
        audit=audit,
        staff_ids=manifest.staff_ids,
    )
    office = run_office(
        suite_id=manifest.suite_id,
        mode=options.mode,
        run_id=options.run_id,
        model_id=model_id,
        config_digest=config_digest,
        git_sha=options.git_sha,
    )

    status, exit_code = _ci_status(office)
    report_path = output_dir / f"{options.run_id}.json"
    payload = _office_summary(office, status=status, exit_code=exit_code)
    _write_summary_once(report_path, payload)
    return EvaluationCLIResult(
        run_id=office.run_id,
        mode=office.mode,
        status=status,
        ready=office.ready,
        sample_size=sum(report.sample_size for report in office.reports),
        report_path=report_path,
        reports_db_path=reports_db_path,
        exit_code=exit_code,
    )


def _ci_status(report: OfficeEvaluationReport) -> tuple[str, int]:
    if report.mode is EvaluationRunMode.CONTRACT:
        passed = all(
            score.passed
            for staff_report in report.reports
            for score in staff_report.scores
        ) and not any(staff_report.governance_violations for staff_report in report.reports)
        return ("contract_passed", 0) if passed else ("contract_failed", 1)
    return ("production_ready", 0) if report.ready else ("not_ready", 1)


def _config_digest(config: ControlPlaneConfig) -> str:
    rendered = json.dumps(
        config.redacted_summary(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _office_summary(
    report: OfficeEvaluationReport,
    *,
    status: str,
    exit_code: int,
) -> dict[str, object]:
    staff = []
    for item in report.reports:
        staff.append(
            {
                "staff_id": item.staff_id,
                "report_id": item.report_id,
                "sample_size": item.sample_size,
                "correctness_rate": item.correctness_rate,
                "safety_pass_rate": item.safety_pass_rate,
                "recovery_rate": item.recovery_rate,
                "blocked_rate": item.blocked_rate,
                "p50_latency_ms": item.p50_latency_ms,
                "p95_latency_ms": item.p95_latency_ms,
                "average_attempts": item.average_attempts,
                "failed_case_ids": list(item.failed_case_ids),
                "governance_violations": list(item.governance_violations),
                "readiness_ready": item.readiness.ready,
                "readiness_status": item.readiness.status,
                "readiness_failures": [failure.value for failure in item.readiness.failures],
            }
        )
    return {
        "run_id": report.run_id,
        "suite_id": report.suite_id,
        "dataset_digest": report.dataset_digest,
        "mode": report.mode.value,
        "status": status,
        "ready": report.ready,
        "exit_code": exit_code,
        "sample_size": sum(item.sample_size for item in report.reports),
        "staff_count": len(report.reports),
        "measured_at": report.measured_at.isoformat(),
        "staff": staff,
    }


def _write_summary_once(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("evaluation summary already exists with different content")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nemotron.staff.evaluation_cli",
        description="Run deterministic contract or isolated live Staff Gold evaluation.",
    )
    parser.add_argument("mode", choices=tuple(mode.value for mode in EvaluationRunMode))
    parser.add_argument("--eval-root", default="evals")
    parser.add_argument("--output-dir", default="evaluation-reports")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--git-sha")
    parser.add_argument("--model-id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _parser().parse_args(argv)
    options = EvaluationCLIOptions(
        mode=EvaluationRunMode(args.mode),
        eval_root=Path(args.eval_root),
        output_dir=Path(args.output_dir),
        run_id=args.run_id,
        git_sha=args.git_sha or (environ or os.environ).get("GITHUB_SHA"),
        model_id=args.model_id,
    )
    try:
        config = None
        if options.mode is EvaluationRunMode.LIVE:
            config = ControlPlaneConfig.from_env(environ or os.environ)
        result = run_evaluation_command(options, config=config)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "evaluation_error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=err,
        )
        return 2

    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "mode": result.mode.value,
                "status": result.status,
                "ready": result.ready,
                "sample_size": result.sample_size,
                "report_path": str(result.report_path),
                "reports_db_path": str(result.reports_db_path),
                "exit_code": result.exit_code,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=out,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
