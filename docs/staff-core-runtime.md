# Staff Runtime Foundation

Phase 3 adds durable runtime state without weakening Staff Core governance.

## Components

- `domain/runtime.py`: MemoryEntry, Goal, PlanProposal, PlanStep, WorkItem, and lifecycle invariants.
- `application/memory.py`: scoped shared memory with private, department, and organization visibility.
- `application/goals.py`: governed goal creation and completion.
- `application/work_queue.py`: inbox, atomic claim, and optimistic-concurrency completion.
- `application/planning.py`: non-executing planning proposal and trusted plan acceptance boundary.
- `application/runtime_ports.py`: persistence, queue, and planning ports.
- `adapters/sqlite_runtime.py`: durable SQLite implementation with WAL, transactions, dependency-aware claiming, and atomic plan acceptance + work enqueue.
- `adapters/nemotron_planner.py`: OpenAI-compatible Nemotron planning adapter using the standard library only.

## Non-bypass rule

A WorkItem coordinates work; it is not a tool call and grants no execution authority. External mutations still require the original governed task lifecycle:

`Evidence -> Decision -> Approval Gate -> Execution -> Verification`

Nemotron may only return a `PlanProposal`. It cannot choose staff, approve, execute tools, provide credentials, or write directly to the queue. `AcceptPlan` performs trusted staff assignment and revalidates permissions and organization scope before atomically queueing work.

## Memory boundaries

- `PRIVATE`: owner only.
- `DEPARTMENT`: staff in the same department.
- `ORGANIZATION`: all authorized staff in that organization.

Memory reads are filtered in the application layer after the caller is authenticated through StaffRepository and placed in the Organization aggregate.

## Concurrency

SQLite work claiming uses `BEGIN IMMEDIATE` and a version predicate so one queued item cannot be successfully claimed twice. Completion requires the assigned staff id, `CLAIMED` status, and the expected version. Plan acceptance is atomic with all generated WorkItems; partial queue creation is rolled back.

## Configuration

Nemotron credentials and endpoint values are supplied at composition time through `NemotronPlannerConfig`. No credentials are stored in Domain or Application code.
