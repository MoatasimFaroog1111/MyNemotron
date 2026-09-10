# MyNemotron Staff Core Architecture

## Purpose

Staff Core is the governed application kernel for a coordinated AI workforce. It is intentionally separate from model providers, web frameworks, databases, queues, ERP systems, email providers, and UI code.

## Dependency Rule

Dependencies point inward only:

```text
Infrastructure / Adapters -> Application -> Domain
```

The Domain contains business rules and invariants. The Application layer coordinates use cases through Ports. External systems implement those Ports later.

## Mandatory task lifecycle

Every externally mutating task follows this sequence:

```text
Evidence -> Decision -> Approval Gate -> Execution -> Verification
```

Low-risk tasks may skip the approval gate only when the active GovernancePolicy explicitly allows that. They never skip evidence, decision, execution gating, or verification.

## Security and governance invariants

- Least privilege: staff act only through explicit Role permissions scoped by action, resource, and maximum risk.
- No self-approval.
- High and critical risk tasks require independent verification under the conservative policy.
- External side effects are behind ActionExecutorPort and are invoked only after `Task.assert_ready_for_execution()` succeeds.
- Every application use case emits an immutable AuditEvent.
- Secrets and provider credentials do not belong in Domain or Application code.
- Domain and Application contain no FastAPI, database, Odoo, Gmail, GitHub, LangGraph, Nemotron runtime, or vendor SDK imports.

## Initial bounded context

```text
src/nemotron/staff/
  domain/
    model.py          Entities, value objects, state machine, governance policy
  application/
    ports.py          Repository, clock, audit, id, and external execution ports
    use_cases.py      Create, assign, evidence, decision, approve, execute, verify
```

## Next layers

Adapters should be added in separate packages and must implement the application Ports rather than being imported by the core. Planned areas include persistent repositories, model-backed decision workers, shared memory, tool connectors, human approval channels, task queues, and the Chief-of-Staff orchestrator.
