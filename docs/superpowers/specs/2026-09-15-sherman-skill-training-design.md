# Sherman Skill Training Design

## Goal

Add a real staff persona named **الشيرمان** to the existing AI office. Clicking Sherman opens a training workspace that accepts regular files and compressed skill packages, especially ZIP and RAR. A package may contain multiple skills; each valid skill is discovered independently, validated, versioned, matched to suitable employees, and activated only after explicit user approval.

## Decision

Use a **Skill Registry + Training Assignments** architecture. Training means governed skill ingestion and runtime assignment, not model fine-tuning and not automatic execution of package code.

## User Experience

- Sherman appears as a real office employee with a stable `staff_id` and hotspot.
- Clicking Sherman opens **مركز تدريب الموظفين**.
- The workspace supports drag-and-drop and file browsing.
- Archive formats initially supported: `.zip`, `.rar`, `.7z`, `.tar`, `.tar.gz`, `.tgz`.
- Other ordinary files may be uploaded as reference material but are not automatically treated as executable skills.
- Archive packages are scanned recursively within a bounded depth.
- Every discovered `SKILL.md` is shown as an independent skill card even when multiple skills come from one archive.
- Each card shows name, description, package digest, validation state, source package, proposed employees, active version, and training status.
- User actions:
  - `تدريب المقترحين`
  - `تدريب الجميع`
  - manual employee override before activation
  - deactivate / reactivate a skill version
  - inspect validation or rejection reasons

## Hybrid Assignment Policy

Sherman analyzes each validated skill and proposes the employees most likely to benefit based on role, department, job title, existing assignments, and skill metadata. No proposal is activated automatically. The user confirms either the suggested set or all active employees.

The assignment engine must be deterministic for the same skill metadata and staff roster. If confidence is low, Sherman proposes no automatic targets and requires manual selection.

## Clean Architecture Boundaries

### Domain

Introduce focused domain concepts:

- `SkillPackage`
- `SkillDefinition`
- `SkillVersion`
- `SkillValidationResult`
- `TrainingRecommendation`
- `TrainingAssignment`
- `TrainingStatus`

The domain must not depend on HTTP, archive libraries, SQLite, frontend code, or subprocesses.

### Application Use Cases

- `ImportSkillPackage`
- `DiscoverSkills`
- `RecommendTrainingTargets`
- `AssignSkillTraining`
- `DeactivateSkillVersion`
- `ListSkillTrainingState`

Use cases depend only on ports.

### Ports

- `ArchiveInspector`
- `SkillDefinitionParser`
- `SkillRegistryRepository`
- `TrainingAssignmentRepository`
- `StaffDirectory`
- `SkillRecommendationEngine`
- `AuditSink`

### Adapters

- safe ZIP/TAR inspector using Python standard libraries where practical
- isolated RAR/7z adapter behind `ArchiveInspector`
- SQLite-backed skill registry and assignment repositories
- adapter from the existing staff repository to `StaffDirectory`
- HTTP/control-plane adapter
- browser UI adapter in the existing frontend

## Skill Package Contract

A discoverable skill must contain `SKILL.md` with YAML frontmatter including at minimum:

```yaml
---
name: example-skill
description: What this skill teaches employees to do.
---
```

The parser preserves the skill instructions and permitted supporting resources as inert content. Scripts and binaries may be stored as package resources but are never executed during import or training.

A single archive may yield zero, one, or many independent `SkillDefinition` records.

## Versioning and Provenance

Each uploaded source package receives SHA-256 provenance. Each discovered skill version also receives a stable content digest based on its normalized definition and allowed resources.

Store:

- source filename
- archive/package SHA-256
- skill content SHA-256
- imported-at timestamp
- importing actor
- skill name and description
- source-relative path
- active/inactive state
- superseded version link when applicable

Duplicate skill content is idempotent and must not create duplicate assignments.

## Security Requirements

Archive handling is hostile-input handling.

Mandatory controls:

- identify actual archive type rather than trusting the extension alone
- reject absolute paths and path traversal (`..`)
- reject symlink / hard-link extraction targets unless represented as inert metadata
- never extract outside an isolated workspace
- cap compressed upload size
- cap total uncompressed bytes
- cap file count
- cap individual file size
- cap nested archive depth
- detect duplicate-entry path collisions
- reject encrypted/password-protected archives in v1
- reject malformed `SKILL.md` frontmatter
- never run `.py`, `.sh`, `.ps1`, `.bat`, `.cmd`, `.exe`, shared libraries, installers, macros, or other executable content during import/training
- never call a package-provided install hook automatically
- audit every import, rejection, activation, deactivation, and assignment

Archive-bomb, Zip-Slip, RAR path traversal, and nested archive abuse must have explicit regression tests.

## Training Semantics

`trained` means the employee runtime can resolve and load the approved skill through the Skill Registry when relevant to a task. It does **not** modify model weights.

Skill resolution must respect:

- employee identity
- active assignment
- active skill version
- existing permission model
- capability gates
- least privilege

A skill cannot grant permissions that the employee does not already possess. Skill instructions are guidance, not authority escalation.

## Sherman Staff Identity

Add a dedicated staff member, for example:

- `staff_id`: `staff-sherman-trainer`
- display name: `الشيرمان`
- department: training / enablement
- role: skill trainer / capability enablement

Sherman's own permissions should allow reading the staff roster, importing/validating skill packages, proposing assignments, and writing governed training assignments. Sherman must not inherit arbitrary production tool execution rights simply because a skill package contains instructions for those tools.

## Control-Plane API

Use authenticated, rate-limited endpoints consistent with the existing control plane. Suggested contract:

- `POST /api/v1/skills/import`
- `GET /api/v1/skills`
- `GET /api/v1/skills/{skill_id}`
- `POST /api/v1/skills/{skill_id}/recommendations`
- `POST /api/v1/skills/{skill_id}/assignments`
- `POST /api/v1/skills/{skill_id}/deactivate`

Import returns discovered skills and validation results. Assignment is a separate explicit write action.

## Frontend Integration

Extend the current office frontend rather than introducing a separate application.

Sherman gets a hotspot/person entry matching the existing office interaction model. The training panel should display these states clearly:

`Uploading -> Scanning -> Validated -> Awaiting Approval -> Training Assigned -> Active`

Failure states must be explicit and non-destructive:

`Rejected`, `Unsupported`, `Malformed`, `Unsafe`, `Duplicate`.

Do not show "trained" before the assignment transaction is persisted and verified by a read-back.

## Data Consistency

Skill import and training assignment are separate transactions.

- Failed import creates no active skill.
- Failed assignment creates no partial employee-training set.
- Multi-employee assignment is atomic where practical; otherwise use a durable operation with explicit per-employee state and no false global success.
- Duplicate requests must be idempotent.

## Testing Strategy

Implementation follows TDD: RED -> GREEN -> REFACTOR.

Required tests include:

- Sherman default staff identity and placement
- ZIP single-skill import
- ZIP multi-skill discovery
- RAR multi-skill discovery through the archive port
- invalid/missing YAML frontmatter
- nested skill folders
- duplicate digest idempotency
- Zip-Slip/path traversal rejection
- symlink/hard-link rejection
- archive bomb limits
- file-count and uncompressed-size limits
- encrypted archive rejection
- executable resources never executed
- deterministic employee recommendations
- low-confidence recommendations require manual choice
- `تدريب المقترحين`
- `تدريب الجميع`
- manual override
- assignment atomicity/idempotency
- capability/permission non-escalation
- frontend upload and state transitions
- authenticated/rate-limited API behavior
- audit events

## Rollout

1. Domain + safe archive inspection + registry with tests.
2. Assignment/recommendation engine with tests.
3. Control-plane API with auth/rate-limit tests.
4. Sherman staff identity and frontend training panel.
5. End-to-end fixture: a GitHub-style archive containing multiple valid skills.
6. Full CI and production smoke test before merge.

## Non-Goals for v1

- model weight fine-tuning
- automatic execution of skill scripts
- automatic package installation
- granting new production permissions from skill metadata
- training employees without explicit user approval

## Acceptance Criteria

The feature is acceptable when a user can click Sherman, upload a supported archive containing multiple GitHub-style skills, see each valid skill separated, see deterministic employee recommendations, choose suggested employees or everyone, approve training, and then verify that only the approved employees can resolve the assigned active skill version. Unsafe archives fail closed with an auditable reason and no code execution.