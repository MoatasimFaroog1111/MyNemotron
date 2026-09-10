# MyNemotron Staff Organization Core

## Purpose

This layer turns the governed Staff Core into an organization rather than a collection of independent agents.

It adds:

- a staff registry contract;
- departments and nested departments;
- one active organization placement per staff member;
- manager chains with cycle prevention;
- an appointed Chief of Staff;
- organization-aware delegation;
- deterministic Chief-of-Staff candidate proposals.

## Dependency rule

Dependencies still point inward only:

```text
Infrastructure / Adapters -> Application -> Domain
```

The organization domain has no model-provider, framework, database, queue, ERP, email, browser, or vendor SDK dependency.

## Staff registry

`StaffRepository` is the registry Port. It supports `get`, `save`, and `list_all`.

`RegisterStaff` enforces two controls before a member can be added:

1. the registrar must have explicit `staff.register` permission;
2. the registrar cannot grant permissions or approval authority beyond their own authority.

Bootstrap of the first trusted owner/admin is intentionally an infrastructure concern and is not exposed as an unrestricted application use case.

## Organization structure

`Organization` is an immutable aggregate containing departments, placements, and the Chief of Staff appointment.

The aggregate rejects:

- duplicate department ids;
- duplicate active placements;
- missing parent departments;
- missing managers;
- department cycles;
- management cycles;
- self-management;
- a Chief of Staff who has no placement.

## Delegation rules

Organization-wide delegation is not inferred from prompt text.

A delegation requires:

1. the delegator is active;
2. the delegator has explicit `delegate` permission for the task resource and risk;
3. the delegatee is active;
4. the delegatee is permitted to perform the task action/resource/risk;
5. the organization hierarchy authorizes that delegator-to-delegatee relationship;
6. the task is still in the evidence stage.

The appointed Chief of Staff may delegate across the organization. Other managers may delegate only within their management chain.

## Chief of Staff orchestrator

`ChiefOfStaffOrchestrator` is deliberately a proposal service, not an autonomous side-effect executor.

It returns a deterministic pool of active, eligible staff for a task and optional department scope. It does not:

- execute external tools;
- bypass task governance;
- approve work;
- verify work;
- invent permissions;
- mutate the task.

The selected candidate is assigned through `DelegateTask`, which applies the organization and permission checks again at the write boundary.

## Audit

Organization and staff lifecycle changes emit immutable governance audit records. Existing task lifecycle audit events remain backward compatible.

## Next layer

The next safe layer can add adapters for persistent staff/organization repositories and then a model-backed planning worker behind an application Port. Model output must remain advisory until validated by deterministic domain rules.
