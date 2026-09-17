from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from nemotron.staff.domain.staff_evaluation import EvaluationRunMode
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
    target.add_argument("--staff", help="Reserved for a single-staff run; office CI uses --all.")
    parser.add_argument("--mode", required=True, choices=tuple(mode.value for mode in EvaluationRunMode))
    parser.add_argument("--suite", default="gold-v1")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--eval-root", default="evals")
    parser.add_argument("--output-dir", default="evaluation-reports")
    parser.add_argument("--run-id")
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
    env = environ or os.environ
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _parser().parse_args(argv)

    if args.staff:
        print("single-staff execution is not exposed by this compatibility entry point yet", file=err)
        return 2
    if args.suite != "gold-v1":
        print(f"unsupported evaluation suite: {args.suite}", file=err)
        return 2

    mode = EvaluationRunMode(args.mode)
    run_id = args.run_id or _default_run_id(mode, env)
    try:
        result = run_evaluation_command(
            EvaluationCLIOptions(
                mode=mode,
                eval_root=Path(args.eval_root),
                output_dir=Path(args.output_dir),
                run_id=run_id,
                git_sha=args.git_sha or env.get("GITHUB_SHA"),
                model_id=args.model_id,
            )
        )
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
