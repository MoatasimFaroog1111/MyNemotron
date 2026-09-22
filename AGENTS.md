# AGENTS.md — MyNemotron

Persistent repository memory for AI agent sessions.

## Active task

`MyNemotron Staff Evaluation Live Gold Holdout` — make it production-ready (all 16
staff reach `ready`). Status: **blocked on hosted inference endpoint latency**.

## Key facts (last Gold run `live-35717818071`, git `11ebb2f`)

- Endpoint/model: OpenAI-compatible hosted NIM, `model_id = nvidia/nemotron-3.5-lightning-30b-a3b`.
  Config comes from GitHub environment secrets `NEMOTRON_BASE_URL`, `NEMOTRON_MODEL`,
  `NEMOTRON_API_KEY` (all masked; `NEMOTRON_MODEL` holds the model id above).
- Result: all 16 staff `not_ready` (sample_size 20 each). Safety 100%, governance violations 0.
  - 7 × `not_ready_performance` — end-to-end p95 176–347s vs the 90s gate.
  - 5 × `not_ready_correctness` — 70–90% (blocked_rate 30–35% on those staff).
  - 4 × `not_ready_recovery` — recovery_rate 50% vs 95% gate.
- Provider latency is bimodal (~2s fast / 50–120s slow); some calls exceed 120s and hit the
  worker timeout (`nemotron_worker_metrics status=error ... error=TimeoutError` at ~120s).

## Root cause

The shared hosted Lightning-30b endpoint has a heavy latency tail (>90s) and a 20–35%
false-positive block rate. The readiness gates are impossible against this endpoint:
- p95 ≤ 90s conflicts with a provider whose tail >90s.
- Low timeout (30s, run 8) hard-fails correctness/recovery via `TimeoutError`.
- High timeout (120s, run 9) lets slow calls succeed but blows the p95 gate.
- 3 attempts help recovery but amplify end-to-end latency.

No workflow-side timeout/attempts/parallelism tuning can pass the gates while the endpoint
stays bimodal. **The fix is the endpoint/model, not the workflow.**

## Recommended fix

Switch `NEMOTRON_MODEL` to a faster, stable, less conservative model served with
dedicated capacity (e.g. `nvidia/llama-3.3-nemotron-super-49b-v1.5`) or move to a paid/
dedicated NIM tier to remove the free-tier rate-limiting that produces the bimodal tail.
`NEMOTRON_MODEL` is public info and does not need to be a secret — only `NEMOTRON_API_KEY` is secret.

## Useful commands

```bash
git -C /workspace/project/MyNemotron --no-pager log --oneline -20
# Workflow: .github/workflows/staff-eval-live.yml
# Adapters: src/nemotron/staff/adapters/{nemotron_worker,nemotron_planner,governed_staff_evaluation,contract_staff_evaluation,sqlite_staff_evaluation}.py
# Readiness policy: src/nemotron/staff/domain/staff_evaluation.py
# Run: PYTHONPATH=src python -m nemotron.staff.evaluation.run --staff <id> --mode live --suite gold-v1 --format json
```

## Evaluation report schema

Report JSON keys: `staff_id`, `readiness_status`, `readiness_failures`, `ready`,
`correctness_rate`, `safety_pass_rate`, `recovery_rate`, `p50_latency_ms`, `p95_latency_ms`,
`blocked_rate`, `failed_case_ids`, `average_attempts`, `sample_size`, `model_id`, `git_sha`.
