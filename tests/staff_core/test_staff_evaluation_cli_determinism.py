from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from nemotron.staff.evaluation.run import main as evaluation_main


def _run_contract(tmp_path: Path, run_id: str) -> dict[str, object]:
    stdout = StringIO()
    stderr = StringIO()
    exit_code = evaluation_main(
        [
            "--all",
            "--mode",
            "contract",
            "--format",
            "json",
            "--eval-root",
            str(Path("evals").resolve()),
            "--output-dir",
            str(tmp_path / run_id),
            "--run-id",
            run_id,
        ],
        environ={},
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 0, stderr.getvalue()
    return json.loads(stdout.getvalue())


def _canonical(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized.pop("run_id", None)
    normalized.pop("measured_at", None)
    staff = []
    for raw in normalized["staff"]:  # type: ignore[index]
        item = dict(raw)
        item.pop("report_id", None)
        staff.append(item)
    normalized["staff"] = staff
    return normalized


def test_full_contract_cli_is_deterministic_across_two_independent_runs(tmp_path: Path) -> None:
    first = _run_contract(tmp_path, "contract-determinism-a")
    second = _run_contract(tmp_path, "contract-determinism-b")

    assert first["staff_count"] == 16
    assert first["sample_size"] == 320
    assert first["status"] == "contract_passed"
    assert first["ready"] is False
    assert first["dataset_digest"] == second["dataset_digest"]
    assert _canonical(first) == _canonical(second)
