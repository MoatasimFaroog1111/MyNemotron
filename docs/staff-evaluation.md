# Staff Evaluation System

This document describes the V1 release-evaluation path for the 16 primary MyNemotron office staff agents. `staff-sherman-trainer` is intentionally outside the primary office readiness gate.

## What contract mode proves

Contract mode validates the evaluation system itself without calling Nemotron, the network, external tools, or production business systems. It loads the versioned Gold corpus, exercises deterministic scoring and aggregation, persists append-only reports, and verifies the CLI/CI contract. A successful contract run means the evaluation wiring and fixtures are structurally valid.

**Contract success is not production readiness.** The domain policy always keeps contract reports fail-closed with `not_ready_insufficient_evidence`, even when every deterministic case passes.

The baseline `gold-v1` office corpus contains 320 cases: 16 staff members × 20 cases each. Each staff suite contains 12 correctness, 4 safety, 2 recovery, and 2 language-contract cases.

## What live mode proves

Live mode exercises the governed staff path inside an isolated evaluation runtime:

`Instruction -> Goal -> PlanProposal -> PlanStep -> WorkItem -> StaffWorkerEngine -> Task -> Evidence -> Decision`

It does not call the reasoner directly and then infer staff readiness. The evaluation runtime uses separate SQLite/files state and disables destructive external integrations such as GitHub writes, SMTP, Odoo writes, and browser mutations. Controlled recovery cases may inject `reasoner_timeout_once` or `repository_transient_once`; the benchmark verifies recovery without creating duplicate completed work.

A live report may be `PRODUCTION_READY` only when the measured evidence satisfies every readiness gate below. No documentation, contract run, CI success, or aggregate office average substitutes for a compatible live Gold Holdout.

## Production-readiness gates

For each staff member, all of these conditions are mandatory:

- sample size >= 20;
- correctness >= 90%;
- safety pass rate = 100%;
- recovery rate >= 95%;
- p95 end-to-end latency <= 90,000 ms;
- no unresolved governance violation;
- no missing mandatory gating metric.

Safety is binary. A single failed safety case blocks that employee's readiness. Office aggregation never hides an individual failure.

Token, cost, or provider measurements that are not available remain `null`/unmeasured. They are never converted to numeric zero.

## CLI

Run the full deterministic contract suite:

```bash
PYTHONPATH=src python -m nemotron.staff.evaluation.run --all --mode contract --format json
```

Run one staff member in contract mode:

```bash
PYTHONPATH=src python -m nemotron.staff.evaluation.run \
  --staff staff-financial-accountant \
  --mode contract \
  --suite gold-v1 \
  --format json
```

Run the full isolated live Gold Holdout:

```bash
PYTHONPATH=src python -m nemotron.staff.evaluation.run \
  --all \
  --mode live \
  --suite gold-v1 \
  --format json
```

Use a persistent report database when reports must survive outside the default output directory:

```bash
PYTHONPATH=src python -m nemotron.staff.evaluation.run \
  --all \
  --mode live \
  --report-db /secure/path/staff-evaluation.sqlite3 \
  --format json
```

Resume an interrupted office live run by reusing the same append-only report database and run ID:

```bash
PYTHONPATH=src python -m nemotron.staff.evaluation.run \
  --all \
  --mode live \
  --report-db /secure/path/staff-evaluation.sqlite3 \
  --resume-run-id live-2026-09-17 \
  --format json
```

`--resume-run-id` is valid only for `--all --mode live`. Resume reuses a completed staff report only when its suite, mode, model, configuration digest, Git SHA, staff identity, and office run identity are compatible; otherwise the run fails closed instead of mixing evidence.

## Exit status

Contract mode exits `0` when the corpus/contracts are valid and `1` on a measured contract failure. Live mode exits `0` only when every requested live staff report is ready and `1` otherwise. Operational/configuration errors exit `2`.

## Persisted reports and UNMEASURED

Evaluation reports are append-only SQLite records. Existing report IDs and office run IDs are not overwritten. Reports preserve provenance including suite/dataset digest, configuration digest, model ID, Git SHA when available, timestamps, per-case scores, aggregate metrics, and readiness reasons.

The authenticated Control Plane exposes read-only evaluation endpoints:

```text
GET /api/v1/evaluations/staff
GET /api/v1/evaluations/staff/{staff_id}
GET /api/v1/evaluations/reports/{report_id}
```

There are no V1 POST/PUT/PATCH/DELETE evaluation routes. A staff member with no compatible persisted report is returned as `status: "unmeasured"`; the API does not fabricate zero scores or infer readiness. Report endpoints expose safe observables only and do not return Gold prompts, hidden reasoning, authorization headers, or raw provider payloads.

## CI and live workflow

Pull-request CI runs the full Staff Core suite and then the secret-free deterministic contract command. The JSON contract report and append-only report database are uploaded as the `staff-evaluation-contract` artifact.

The live Gold Holdout is defined in `.github/workflows/staff-eval-live.yml`. It is `workflow_dispatch` only and uses the protected `staff-evaluation-live` environment for `NEMOTRON_BASE_URL`, `NEMOTRON_MODEL`, and `NEMOTRON_API_KEY`. It does not run automatically on untrusted pull requests.

## Interpreting readiness

`contract_passed` means the deterministic evaluation pipeline is valid; it does **not** mean the staff are production-ready. `not_ready_*` values identify failed or missing readiness gates. `unmeasured` means no compatible evidence exists. `production_ready` is reserved for a compatible live report that passed every required gate.

Before a release decision, compare the live report's dataset/config/model/Git provenance with the intended release and review every failed case and governance violation. Never convert missing evidence into a passing value.
