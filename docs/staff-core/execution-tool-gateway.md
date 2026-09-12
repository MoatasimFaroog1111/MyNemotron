# Execution & Tool Gateway

## Purpose

The Tool Gateway is the only supported boundary from an execution-ready Staff Core task to an external system. Agents, planners, and workers never receive GitHub, email, Odoo, browser, filesystem, or credential objects directly.

```text
Staff Core Task
  -> immutable ToolExecutionIntent
  -> approval gate (for mutating operations)
  -> short-lived signed CapabilityToken
  -> durable idempotency reservation
  -> registered guarded Tool Adapter
  -> durable ToolExecutionReceipt
  -> Task.record_execution()
  -> verification
```

## Security invariants

- Tool payload is canonicalized and SHA-256 hashed before execution.
- A task may have only one immutable execution intent. A different payload requires a different/reviewed task lifecycle.
- For mutating operations, the intent must exist before the approval timestamp. Approval of one payload cannot authorize a later payload.
- Capability tokens are HMAC-signed, short-lived, and scoped to task, staff member, tool, operation, arguments digest, and idempotency key.
- Every concrete adapter independently verifies the capability. Direct adapter calls without a valid token fail.
- Tool definitions enforce resource, task-action, minimum-risk, and mutating/read-only boundaries.
- Idempotency is durable. Unknown/ambiguous external failures remain PROCESSING and block automatic retry to avoid duplicate side effects.
- Only failures explicitly proven to have occurred before a side effect release the idempotency reservation.
- Tool results are returned as structured `output_json`; audit records store only reference/summary metadata rather than the full result body.
- Secrets are injected through runtime configuration only and are never stored in Domain/Application objects, tool intents, logs, or source code.

## Initial guarded adapters

- `GitHubToolAdapter`: allowlisted repositories; read issue, create issue, comment on issue.
- `SMTPEmailToolAdapter`: send email through configured SMTP; optional recipient-domain allowlist; deterministic Message-ID from the idempotency key.
- `OdooToolAdapter`: JSON-RPC with model allowlist; read records, create record, write record. Mutations require HIGH-risk approved tasks.
- `BrowserToolAdapter`: GET only, explicit host allowlist, redirects blocked, response-size limit, private/loopback IP literals blocked.
- `FilesToolAdapter`: root-scoped UTF-8 text reads/writes, traversal prevention, size limits, atomic replace for writes.

## Composition

Infrastructure should create one `HMACCapabilityAuthority` from a runtime secret of at least 32 bytes, create a `SQLiteGatewayStore`, register only the tools enabled for that deployment in `InMemoryToolRegistry`, and inject the registry into `PrepareToolExecution` and `ExecuteToolTask`.

A production deployment should keep provider credentials in environment/secret-manager configuration. Do not pass credentials as tool arguments.

## Approval sequence for mutations

```text
1. Worker records Evidence + Decision
2. PrepareToolExecution freezes exact tool + operation + arguments digest
3. Human/authorized role approves the task
4. ExecuteToolTask verifies intent.prepared_at <= approval.decided_at
5. Gateway issues a short-lived capability and reserves idempotency
6. Guarded adapter independently validates capability and performs the external action
7. Receipt is stored before Task is advanced to VERIFYING
8. Independent verification runs according to GovernancePolicy
```

Read-only LOW-risk operations may execute without an Approval object only when the Staff Core task is already `READY_FOR_EXECUTION`. They still require evidence, decision, assignment, permission, immutable intent, capability validation, idempotency, and audit.

## Deliberate limitations

The browser adapter is an allowlisted HTTP GET connector, not a browser-automation engine. A later Playwright/browser-computer adapter must implement the same `ToolAdapterPort` and capability validation and should not weaken this gateway.
