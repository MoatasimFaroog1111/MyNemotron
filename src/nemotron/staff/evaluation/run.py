from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
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
from nemotron.staff.application.staff_evaluation import (
    RunStaffEvaluation,
    RunStaffEvaluationRequest,
)
from nemotron.staff.control_plane.config import ControlPlaneConfig
from nemotron.staff.control_plane.runtime import UtcClock
from nemotron.staff.domain.staff_evaluation import EvaluationRunMode, StaffEvaluationReport
from nemotron.staff.evaluation_cli import EvaluationCLIOptions, run_evaluation_command


def _default_run_id(mode: EvaluationRunMode, environ: Mapping[str, str]) -> str:
    github_run_id = environ.get("GITHUB_RUN_ID", "").strip()
    if github_run_id:
        return f"{mode.value}-{github_run_id}"
    github_sha = environ.get("GITHUB_SHA", "").strip()
    if github_sha:
        return f"{mode.value}-{github_sha[:12]}"
    return f"{mode.value}-local"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nemotron.staff.evaluation.run",
        description="Run the governed staff evaluation suite.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Evaluate every staff member in the office manifest.")
    target.add_argument("--staff", help="Evaluate one staff member from the selected suite.")
    parser.add_argument("--mode", required=True, choices=tuple(mode.value for mode in EvaluationRunMode))
    parser.add_argument("--suite", default="gold-v1")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--eval-root", default="evals")
    parser.add_argument("--output-dir", default="evaluation-reports")
    run_identity = parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id")
    run_identity.add_argument("--resume-run-id")
    parser.add_argument("--report-db")
    parser.add_argument("--git-sha")
    parser.add_argument("--model-id")
    return parser


def _config_digest(config: ControlPlaneConfig) -> str:
    rendered = json.dumps(
        config.redacted_summary(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _single_staff_payload(report: StaffEvaluationReport, *, status: str, exit_code: int) -> dict[str, object]:
    return {
        "report_id": report.report_id,
        "staff_id": report.staff_id,
        "suite_id": report.suite.suite_id,
        "dataset_digest": report.suite.dataset_digest,
        "mode": report.mode.value,
        "status": status,
        "ready": report.readiness.ready,
        "exit_code": exit_code,
        "sample_size": report.sample_size,
        "model_id": report.model_id,
        "git_sha": report.git_sha,
        "correctness_rate": report.correctness_rate,
        "safety_pass_rate": report.safety_pass_rate,
        "recovery_rate": report.recovery_rate,
        "blocked_rate": report.blocked_rate,
        "p50_latency_ms": report.p50_latency_ms,
        "p95_latency_ms": report.p95_latency_ms,
        "average_attempts": report.average_attempts,
        "failed_case_ids": list(report.failed_case_ids),
        "governance_violations": list(report.governance_violations),
        "readiness_status": report.readiness.status,
        "readiness_failures": [failure.value for failure in report.readiness.failures],
        "measured_at": report.measured_at.isoformat(),
    }


def _run_single_staff(
    *,
    staff_id: str,
    mode: EvaluationRunMode,
    suite_id: str,
    eval_root: Path,
    output_dir: Path,
    run_id: str,
    git_sha: str | None,
    model_id: str | None,
    report_db_path: Path | None,
    environ: Mapping[str, str],
) -> tuple[dict[str, object], int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = JsonlStaffEvaluationCaseRepository(eval_root.resolve())
    manifest = cases.load_office_manifest()
    if suite_id != manifest.suite_id:
        raise ValueError(f"unsupported evaluation suite: {suite_id}")
    if staff_id not in manifest.staff_ids:
        raise ValueError(f"staff_id is not present in the evaluation manifest: {staff_id}")

    reports_db_path = (
        report_db_path.resolve()
        if report_db_path is not None
        else output_dir.resolve() / "staff-evaluation.sqlite3"
    )
    reports_db_path.parent.mkdir(parents=True, exist_ok=True)
    reports = SQLiteStaffEvaluationReportRepository(reports_db_path)
    audit = SQLiteAuditLog(SQLiteControlStore(reports_db_path))
    clock = UtcClock()

    if mode is EvaluationRunMode.CONTRACT:
        runner = DeterministicContractStaffEvaluationRunner()
        selected_model_id = model_id or "contract-fixture"
        config_digest = hashlib.sha256(b"staff-evaluation-contract-cli-v1").hexdigest()
    else:
        config = ControlPlaneConfig.from_env(environ)
        runner = GovernedStaffEvaluationRunner(
            IsolatedEvaluationRuntimeFactory(
                config,
                evaluation_root=output_dir.resolve() / "runtime" / run_id,
            )
        )
        selected_model_id = model_id or config.nemotron_model
        config_digest = _config_digest(config)

    run_staff = RunStaffEvaluation(
        cases=cases,
        runner=runner,
        reports=reports,
        clock=clock,
        audit=audit,
    )
    report = run_staff(
        RunStaffEvaluationRequest(
            staff_id=staff_id,
            suite_id=suite_id,
            mode=mode,
            report_id=f"{run_id}:{staff_id}",
            model_id=selected_model_id,
            config_digest=config_digest,
            git_sha=git_sha,
        )
    )

    if mode is EvaluationRunMode.CONTRACT:
        passed = all(score.passed for score in report.scores) and not report.governance_violations
        status, exit_code = (("contract_passed", 0) if passed else ("contract_failed", 1))
    else:
        status, exit_code = (("production_ready", 0) if report.readiness.ready else ("not_ready", 1))
    payload = _single_staff_payload(report, status=status, exit_code=exit_code)
    report_path = output_dir.resolve() / f"{run_id}.json"
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    report_path.write_text(encoded, encoding="utf-8", errors="strict")
    return payload, exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    env = environ or os.environ
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _parser().parse_args(argv)

    mode = EvaluationRunMode(args.mode)
    if args.resume_run_id and (not args.all or mode is not EvaluationRunMode.LIVE):
        print("--resume-run-id is valid only with --all --mode live", file=err)
        return 2
    run_id = args.resume_run_id or args.run_id or _default_run_id(mode, env)
    git_sha = args.git_sha or env.get("GITHUB_SHA")
    report_db_path = Path(args.report_db) if args.report_db else None
    try:
        if args.staff:
            payload, exit_code = _run_single_staff(
                staff_id=args.staff,
                mode=mode,
                suite_id=args.suite,
                eval_root=Path(args.eval_root),
                output_dir=Path(args.output_dir),
                run_id=run_id,
                git_sha=git_sha,
                model_id=args.model_id,
                report_db_path=report_db_path,
                environ=env,
            )
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=out)
            else:
                print(
                    f"{mode.value}: {payload['status']}; staff={args.staff}; "
                    f"cases={payload['sample_size']}; ready={str(payload['ready']).lower()}",
                    file=out,
                )
            return exit_code

        cases = JsonlStaffEvaluationCaseRepository(Path(args.eval_root).resolve())
        manifest = cases.load_office_manifest()
        if args.suite != manifest.suite_id:
            raise ValueError(f"unsupported evaluation suite: {args.suite}")
        options = EvaluationCLIOptions(
            mode=mode,
            eval_root=Path(args.eval_root),
            output_dir=Path(args.output_dir),
            run_id=run_id,
            git_sha=git_sha,
            model_id=args.model_id,
            reports_db_path=report_db_path,
        )
        config = ControlPlaneConfig.from_env(env) if mode is EvaluationRunMode.LIVE else None
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

    payload = json.loads(result.report_path.read_text(encoding="utf-8"))
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=out)
    else:
        print(
            f"{result.mode.value}: {result.status}; cases={result.sample_size}; ready={str(result.ready).lower()}",
            file=out,
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
