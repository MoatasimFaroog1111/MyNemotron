from __future__ import annotations

from pathlib import Path


CONTRACT_COMMAND = (
    "PYTHONPATH=src python -m nemotron.staff.evaluation.run "
    "--all --mode contract --format json"
)
LIVE_COMMAND_PREFIX = "PYTHONPATH=src python -m nemotron.staff.evaluation.run"


def test_pr_ci_runs_secret_free_contract_evaluation() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Staff Evaluation Contract" in workflow
    assert CONTRACT_COMMAND in workflow
    assert "staff-evaluation-contract.json" in workflow


def test_live_gold_holdout_is_manual_only_and_not_pull_request_triggered() -> None:
    workflow = Path(".github/workflows/staff-eval-live.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert LIVE_COMMAND_PREFIX in workflow
    assert '--staff "${{ matrix.staff_id }}"' in workflow
    assert "--mode live" in workflow
    assert "--suite gold-v1" in workflow
    assert "--format json" in workflow
    assert "fail-fast: false" in workflow
    assert "max-parallel: 4" in workflow
    assert "NEMOTRON_BASE_URL" in workflow
    assert "NEMOTRON_MODEL" in workflow
    assert "NEMOTRON_API_KEY" in workflow
    assert "staff-evaluation-live-${{ matrix.staff_id }}" in workflow
    assert "Validate and prepare live evaluation environment" in workflow
    assert "Missing required GitHub environment secret:" in workflow
    assert "openssl rand -hex 24" in workflow
    assert "openssl rand -hex 32" in workflow
    assert "STAFF_CONTROL_API_TOKEN=" in workflow
    assert "STAFF_CAPABILITY_HMAC_SECRET=" in workflow


def test_planned_evaluation_module_entry_point_exists() -> None:
    entrypoint = Path("src/nemotron/staff/evaluation/run.py")
    assert entrypoint.is_file()
