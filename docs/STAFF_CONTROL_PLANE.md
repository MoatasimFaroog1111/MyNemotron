# MyNemotron Staff Production Control Plane

The control plane composes Staff Core, persistent runtime memory/goals/work queue, Nemotron planning and worker reasoning, and the governed Tool Gateway into one process.

## Safety model

- LLM adapters never receive tool credentials or tool adapter objects.
- Mutating tool payloads are frozen before approval by `ToolExecutionIntent`.
- Approval, execution, and verification remain separate Staff Core transitions.
- Capability tokens are short-lived and signed; adapters independently validate them.
- Idempotency is durable. Ambiguous side effects remain blocked until reconciled.
- Audit records are append-only and do not contain provider secrets or full tool outputs.
- `/health` is public. All control endpoints require the control-plane bearer token.

## Required environment

```text
STAFF_CONTROL_API_TOKEN=<random token, at least 24 chars>
STAFF_CAPABILITY_HMAC_SECRET=<random secret, at least 32 bytes>
NEMOTRON_BASE_URL=<OpenAI-compatible endpoint>
NEMOTRON_MODEL=<model id>
NEMOTRON_API_KEY=<optional when endpoint requires it>
```

Optional service settings:

```text
STAFF_DATA_DIR=./data/staff
STAFF_FILES_ROOT=./data/staff/files
STAFF_CONTROL_HOST=127.0.0.1
STAFF_CONTROL_PORT=8088
```

Optional guarded connectors are registered only when their configuration is complete:

```text
GITHUB_TOKEN=<runtime secret>
STAFF_GITHUB_ALLOWED_REPOSITORIES=owner/repo,owner/other

STAFF_SMTP_HOST=smtp.example.com
STAFF_SMTP_PORT=587
STAFF_SMTP_USERNAME=<runtime secret user>
STAFF_SMTP_PASSWORD=<runtime secret password>
STAFF_SMTP_FROM=staff@example.com
STAFF_SMTP_ALLOWED_DOMAINS=example.com

STAFF_ODOO_URL=https://example.odoo.com
STAFF_ODOO_DATABASE=<database>
STAFF_ODOO_UID=<numeric uid>
STAFF_ODOO_API_KEY=<runtime secret>
STAFF_ODOO_ALLOWED_MODELS=res.partner,account.move

STAFF_BROWSER_ALLOWED_HOSTS=docs.example.com,api.example.com
```

Do not commit real values. Inject them through the deployment environment or secret manager.

## Start the service

```bash
python -m nemotron.staff.control_plane
```

Core endpoints:

```text
GET  /health
GET  /api/v1/config
GET  /api/v1/approvals
GET  /api/v1/executions
GET  /api/v1/tasks/{task_id}
GET  /api/v1/audit
POST /api/v1/workers/{staff_id}/run
POST /api/v1/tasks/{task_id}/intent
POST /api/v1/tasks/{task_id}/approval
POST /api/v1/tasks/{task_id}/execute
POST /api/v1/tasks/{task_id}/verify
```

Use `Authorization: Bearer <STAFF_CONTROL_API_TOKEN>` for every `/api/v1/*` request.

## End-to-end verification

The CI test starts a local OpenAI-compatible HTTP model endpoint and sends requests through the real `NemotronPlanningAdapter` and `NemotronWorkerReasoningAdapter`. It then performs a real Files Tool side effect through the production capability/idempotency gateway and verifies the resulting file before completing the task.

To run the same flow against a real Nemotron endpoint while keeping the external side effect isolated to a temporary Files root:

```bash
NEMOTRON_BASE_URL=... NEMOTRON_MODEL=... NEMOTRON_API_KEY=... \
  python -m nemotron.staff.control_plane.live_e2e
```

The live E2E runner creates a temporary database and Files root and does not register GitHub, SMTP, Odoo, or Browser connectors.
