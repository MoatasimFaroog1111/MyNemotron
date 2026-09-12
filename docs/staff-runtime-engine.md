# Staff Runtime Engine

## Purpose

Phase 4 turns accepted Staff Runtime work items into governed Staff Core decisions without creating a second execution path.

```text
Inbox -> Atomic Claim -> Trusted Context -> Nemotron Reasoning
      -> Evidence -> Decision -> Approval/Execution Gate
```

The worker stops after the decision handoff. It never approves, executes, or verifies an external action.

## Worker authority boundary

Nemotron receives only:

- the assigned work item,
- the active goal,
- the worker identity and role name,
- the current governed task state,
- memory already visible to that worker.

Memory text is explicitly treated as untrusted data. The reasoning adapter may only return:

- `ready` or `blocked`,
- visible memory ids to use as evidence,
- a decision rationale,
- a non-authoritative work summary.

It cannot change the work action, resource, risk, assignee, approval state, verification state, credentials, or tool calls. Unsupported authority fields fail closed.

## Accepted plan -> governed task

Every accepted `WorkItem` is projected to one deterministic Staff Core task id:

```text
work-task:<work_item_id>
```

Before creating or reusing that task, the runtime verifies:

1. the source plan is accepted,
2. organization and goal ids match,
3. the plan step still matches title/action/resource/risk/dependencies,
4. the assigned worker is active, placed in the organization, and permitted for the action.

This projection adds no new delegation authority; it only preserves the assignment already accepted by the governed planning boundary.

## Evidence rules

The model does not invent evidence records. It may select only `memory_id` values already present in `visible_memory`.

The application writes evidence using the original stored memory content and source reference, not a model rewrite. High and critical risk work requires source-referenced memory. Unsourced high-risk context is blocked for human review.

## Crash and retry behavior

`SQLiteWorkerQueue` keeps a durable attempt counter separate from the base work queue.

- claims are atomic,
- transient reasoning/runtime failures release the item back to `QUEUED`,
- a configurable retry limit moves repeated failures to `BLOCKED`,
- completed Staff Core decisions are detected on retry so the model is not asked to decide twice.

`SQLiteTaskRepository` persists the complete Staff Core `Task` aggregate so evidence and decisions survive process restarts.

## External side effects

The Worker Engine has no `ActionExecutorPort` dependency.

For low-risk work, the resulting Task may reach `READY_FOR_EXECUTION`.
For medium/high/critical work under the conservative policy, it reaches `AWAITING_APPROVAL`.

Only the existing Staff Core use cases may continue from there:

```text
ApproveTask -> ExecuteTask -> VerifyTask
```

This keeps Evidence -> Decision -> Approval -> Execution -> Verification as the only external-mutation path.
