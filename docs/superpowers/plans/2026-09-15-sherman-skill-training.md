# Sherman Skill Training Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Sherman office trainer so users can safely import multi-skill archives, receive deterministic staff recommendations, approve training assignments, and make approved skills available to assigned employees without executing archive code or escalating permissions.

**Architecture:** Use a Skill Registry + Training Assignments design. Domain objects and use cases remain independent of HTTP, SQLite, archive libraries, and the frontend; adapters provide safe archive inspection and persistence; the existing control plane wires the feature into authenticated APIs and the office UI.

**Tech Stack:** Python 3.12, dataclasses, pathlib/zipfile/tarfile, SQLite, existing stdlib HTTP server, vanilla HTML/CSS/JS, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-sherman-skill-training-design.md`

## Global Constraints

- Follow Clean Architecture, SOLID, Dependency Rule, DRY, YAGNI, and TDD RED -> GREEN -> REFACTOR.
- Never execute scripts, binaries, macros, installers, or archive-provided hooks during import or training.
- A skill cannot grant permissions beyond the employee's existing role/capability gates.
- Every archive import, rejection, recommendation, assignment, activation, and deactivation must be auditable.
- Multi-skill archives must produce independent skill definitions.
- Training is never activated automatically; explicit user approval is required.
- Duplicate package/skill content must be idempotent.
- Reject traversal, absolute paths, unsafe links, encrypted archives, path collisions, archive bombs, excessive nesting, excessive file count, and excessive uncompressed bytes.
- Keep `main` unchanged until branch verification and review are complete.

---

## File Structure

**Create**
- `src/nemotron/staff/domain/skills.py` — pure skill/training domain types and validation errors.
- `src/nemotron/staff/application/skill_ports.py` — archive, registry, assignment, staff-directory, recommendation, and audit ports.
- `src/nemotron/staff/application/skill_training.py` — import, recommend, assign, deactivate, and resolve use cases.
- `src/nemotron/staff/adapters/safe_skill_archives.py` — hostile-input archive inspection and `SKILL.md` parsing.
- `src/nemotron/staff/adapters/sqlite_skills.py` — SQLite skill registry and training assignments.
- `src/nemotron/staff/adapters/skill_recommendations.py` — deterministic role/department recommendation engine.
- `tests/staff_core/test_skill_archive_import.py` — archive security and multi-skill discovery.
- `tests/staff_core/test_skill_training_assignments.py` — recommendation, approval, idempotency, permission non-escalation.
- `tests/staff_core/test_sherman_control_plane.py` — runtime/service/API integration.
- `tests/staff_core/test_sherman_frontend_contract.py` — UI contract and upload/training states.

**Modify**
- `src/nemotron/staff/domain/__init__.py` — export skill domain types.
- `src/nemotron/staff/control_plane/default_staff.py` — add `staff-sherman-trainer` and least-privilege training permissions.
- `src/nemotron/staff/control_plane/runtime.py` — construct repositories/use cases and expose them on `ProductionRuntime`.
- `src/nemotron/staff/control_plane/service.py` — convert HTTP-facing payloads into use-case calls and read-back verification.
- `src/nemotron/staff/control_plane/http_api.py` — authenticated/rate-limited skill import/list/recommend/assign/deactivate routes and bounded upload handling.
- `src/nemotron/staff/control_plane/frontend/index.html` — Sherman training panel markup.
- `src/nemotron/staff/control_plane/frontend/app.js` — Sherman hotspot/panel, upload workflow, recommendations and explicit approval actions.
- `src/nemotron/staff/control_plane/frontend/app.css` — training panel/status styles.
- `tests/staff_core/test_default_staff.py` — expect Sherman as the 17th real staff member and verify permissions/placement.

---

### Task 1: Sherman Identity and Pure Skill Domain

**Files:**
- Create: `src/nemotron/staff/domain/skills.py`
- Modify: `src/nemotron/staff/domain/__init__.py`
- Modify: `src/nemotron/staff/control_plane/default_staff.py`
- Test: `tests/staff_core/test_default_staff.py`
- Test: `tests/staff_core/test_skill_training_assignments.py`

**Interfaces:**
- Produces `SkillDefinition`, `SkillVersion`, `SkillValidationResult`, `TrainingRecommendation`, `TrainingAssignment`, `TrainingStatus`, `SkillTrainingError`.
- Produces real staff identity `staff-sherman-trainer` in department `training`.

- [ ] **Step 1: Write failing tests**

Update `test_default_staff_roster_is_complete_and_idempotent` to assert 17 staff members, Sherman presence, training placement, and Sherman-specific permissions while preserving two-permission behavior for ordinary staff.

Add a pure-domain test similar to:

```python
from nemotron.staff.domain.skills import SkillDefinition, TrainingStatus


def test_skill_definition_normalizes_identity_without_authority() -> None:
    skill = SkillDefinition(
        skill_id="accounting-review",
        name="Accounting Review",
        description="Review accounting evidence.",
        instructions="Use evidence first.",
        source_path="skills/accounting/SKILL.md",
    )
    assert skill.skill_id == "accounting-review"
    assert TrainingStatus.ACTIVE.value == "active"
```

- [ ] **Step 2: Verify RED**

Run:
`pytest tests/staff_core/test_default_staff.py tests/staff_core/test_skill_training_assignments.py -q`

Expected: FAIL because Sherman and `nemotron.staff.domain.skills` do not exist yet.

- [ ] **Step 3: Implement minimal domain and Sherman roster change**

Create immutable dataclasses/enums only; no SQLite/HTTP imports. Add Sherman with permissions scoped to:

```python
Permission("read", "staff-directory", RiskLevel.LOW)
Permission("skill.import", "skill-registry", RiskLevel.MEDIUM)
Permission("skill.assign", "skill-training", RiskLevel.MEDIUM)
```

Do not give Sherman wildcard write/tool permissions.

- [ ] **Step 4: Verify GREEN**

Run the same pytest command and require PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nemotron/staff/domain src/nemotron/staff/control_plane/default_staff.py tests/staff_core/test_default_staff.py tests/staff_core/test_skill_training_assignments.py
git commit -m "feat: add Sherman trainer and skill domain"
```

---

### Task 2: Safe Multi-Skill Archive Inspection

**Files:**
- Create: `src/nemotron/staff/application/skill_ports.py`
- Create: `src/nemotron/staff/adapters/safe_skill_archives.py`
- Test: `tests/staff_core/test_skill_archive_import.py`

**Interfaces:**
- Produces `ArchiveInspector.inspect(filename: str, payload: bytes) -> ArchiveInspection`.
- `ArchiveInspection` returns package SHA-256, archive kind, discovered independent skills, inert resource metadata, and validation/rejection details.
- No adapter API exposes an execute/run/install method.

- [ ] **Step 1: Write failing archive tests**

Create in-memory ZIP fixtures using `io.BytesIO` and `zipfile.ZipFile` for:
- one valid `SKILL.md`
- two skills in different directories
- `../escape/SKILL.md` traversal
- duplicate normalized path collision
- oversized uncompressed payload under a deliberately tiny test limit
- malformed/missing YAML frontmatter
- nested archive depth over limit

Example assertion:

```python
inspection = inspector.inspect("skills.zip", payload)
assert [skill.name for skill in inspection.skills] == ["Accounting", "Odoo"]
assert inspection.package_sha256
assert inspection.rejected is False
```

- [ ] **Step 2: Verify RED**

Run:
`pytest tests/staff_core/test_skill_archive_import.py -q`

Expected: FAIL because the archive port/adapter do not exist.

- [ ] **Step 3: Implement safe ZIP/TAR parser and bounded generic archive contract**

Implement ZIP/TAR with standard library. Detect format from magic/content rather than extension. Normalize POSIX paths and reject absolute paths, `..`, links, collisions, encrypted ZIP entries, file-count overflow, per-file overflow, total-uncompressed overflow, and nested archive depth overflow.

Parse `SKILL.md` frontmatter with a deliberately small parser supporting required scalar `name` and `description`; keep instructions/resources inert. RAR/7z are represented behind the same port and must fail as `unsupported` unless an explicitly available safe backend exists; never shell out to package-provided code.

- [ ] **Step 4: Verify GREEN**

Run the archive test file and then `pytest tests/staff_core -q`.

- [ ] **Step 5: Commit**

```bash
git add src/nemotron/staff/application/skill_ports.py src/nemotron/staff/adapters/safe_skill_archives.py tests/staff_core/test_skill_archive_import.py
git commit -m "feat: safely inspect multi-skill archives"
```

---

### Task 3: Registry, Recommendations, and Explicit Training Assignment

**Files:**
- Create: `src/nemotron/staff/adapters/sqlite_skills.py`
- Create: `src/nemotron/staff/adapters/skill_recommendations.py`
- Create: `src/nemotron/staff/application/skill_training.py`
- Test: `tests/staff_core/test_skill_training_assignments.py`

**Interfaces:**
- Produces `ImportSkillPackage`, `RecommendTrainingTargets`, `AssignSkillTraining`, `DeactivateSkillVersion`, `ResolveAssignedSkills`.
- Repository operations are digest-idempotent.
- Assignment takes an explicit target staff-id set; `all_active=True` is translated by the application layer, not the repository.

- [ ] **Step 1: Extend failing tests**

Cover:
- multi-skill import creates independent registry entries
- same skill digest import is idempotent
- finance/Odoo metadata deterministically recommends finance staff before unrelated staff
- low-confidence skill returns empty automatic recommendation
- suggested-target approval assigns only suggested IDs
- all-active approval assigns all active staff except Sherman unless explicitly selected
- duplicate assignment request is idempotent
- deactivated skill cannot resolve at runtime
- skill instructions never mutate `StaffMember.role.permissions`

- [ ] **Step 2: Verify RED**

Run:
`pytest tests/staff_core/test_skill_training_assignments.py -q`

Expected: FAIL on missing repositories/use cases.

- [ ] **Step 3: Implement SQLite schema and use cases**

Use dedicated tables such as `skill_packages`, `skill_versions`, and `skill_assignments`. Persist source/package/content digests, source path, status, timestamps, actor, and supersession. Use one SQLite transaction for a multi-employee assignment request.

Recommendation scoring must be deterministic and transparent, based on normalized skill name/description plus department/role/job-title tokens. Do not use an LLM for v1 recommendation decisions.

- [ ] **Step 4: Verify GREEN**

Run the assignment tests and full staff-core tests.

- [ ] **Step 5: Commit**

```bash
git add src/nemotron/staff/application/skill_training.py src/nemotron/staff/adapters/sqlite_skills.py src/nemotron/staff/adapters/skill_recommendations.py tests/staff_core/test_skill_training_assignments.py
git commit -m "feat: add governed skill registry and training assignments"
```

---

### Task 4: Production Runtime, Service, and Authenticated API

**Files:**
- Modify: `src/nemotron/staff/control_plane/runtime.py`
- Modify: `src/nemotron/staff/control_plane/service.py`
- Modify: `src/nemotron/staff/control_plane/http_api.py`
- Test: `tests/staff_core/test_sherman_control_plane.py`

**Interfaces:**
- `ProductionRuntime` exposes repositories/use cases, not adapter internals to HTTP.
- Service methods: `import_skills`, `list_skills`, `recommend_skill_training`, `assign_skill_training`, `deactivate_skill`.
- UI/API import accepts bounded binary payload plus filename; write routes require existing auth/session and write-rate limits.

- [ ] **Step 1: Write failing integration tests**

Start the existing control-plane test server pattern and assert:
- unauthenticated import is rejected
- valid ZIP import returns two independent skills
- recommendation endpoint returns staff IDs but does not assign
- assignment endpoint persists and read-back verifies target IDs
- duplicate assignment returns the same effective state
- deactivate removes skill from employee resolution
- audit contains import and assignment events

- [ ] **Step 2: Verify RED**

Run:
`pytest tests/staff_core/test_sherman_control_plane.py -q`

Expected: FAIL/404 because routes and runtime wiring do not exist.

- [ ] **Step 3: Implement runtime/service/API wiring**

Add bounded binary-body reader for skill import without weakening `_read_json` limits. Keep upload maximum explicit. Apply same-origin/session checks for UI writes and bearer auth for API writes. Apply existing write rate limiter. Convert all archive/security failures to non-destructive 4xx responses.

- [ ] **Step 4: Verify GREEN**

Run the integration test and all `tests/staff_core`.

- [ ] **Step 5: Commit**

```bash
git add src/nemotron/staff/control_plane/runtime.py src/nemotron/staff/control_plane/service.py src/nemotron/staff/control_plane/http_api.py tests/staff_core/test_sherman_control_plane.py
git commit -m "feat: expose Sherman skill training control plane"
```

---

### Task 5: Sherman Office UI

**Files:**
- Modify: `src/nemotron/staff/control_plane/frontend/index.html`
- Modify: `src/nemotron/staff/control_plane/frontend/app.js`
- Modify: `src/nemotron/staff/control_plane/frontend/app.css`
- Create: `tests/staff_core/test_sherman_frontend_contract.py`

**Interfaces:**
- Clicking `staff-sherman-trainer` opens `مركز تدريب الموظفين` rather than the generic instruction-only experience.
- UI supports drag/drop and browse; accepted archive labels include ZIP, RAR, 7z, TAR, TAR.GZ/TGZ and ordinary reference files.
- Each discovered skill has independent recommendation/selection/training controls.

- [ ] **Step 1: Write failing frontend contract test**

Assert packaged assets contain stable selectors/data attributes for:
- Sherman entry/hotspot
- training panel
- file input/drop zone
- skill cards
- `تدريب المقترحين`
- `تدريب الجميع`
- status strings `Uploading`, `Scanning`, `Validated`, `Awaiting Approval`, `Active`, and failure states

- [ ] **Step 2: Verify RED**

Run:
`pytest tests/staff_core/test_sherman_frontend_contract.py -q`

Expected: FAIL because UI contract is absent.

- [ ] **Step 3: Implement minimal accessible UI**

Reuse the existing office staff data/hotspot model. Add a Sherman-specific panel with explicit file state, per-skill employee checkboxes, recommendation display, and approval buttons. Do not mark `Active` until the assignment POST succeeds and a GET/read-back confirms it.

- [ ] **Step 4: Verify GREEN**

Run frontend contract tests, responsive contract tests, and full staff-core tests.

- [ ] **Step 5: Commit**

```bash
git add src/nemotron/staff/control_plane/frontend tests/staff_core/test_sherman_frontend_contract.py
git commit -m "feat: add Sherman training workspace"
```

---

### Task 6: End-to-End Safety Gate and Branch Verification

**Files:**
- Test: `tests/staff_core/test_sherman_control_plane.py`
- Test: `tests/staff_core/test_skill_archive_import.py`
- Test: `tests/staff_core/test_skill_training_assignments.py`
- Test: `tests/staff_core/test_sherman_frontend_contract.py`

**Interfaces:**
- Final fixture mimics a GitHub-downloaded archive containing at least two skills and one inert script resource.

- [ ] **Step 1: Add final E2E fixture**

Create an in-memory GitHub-style ZIP containing two `SKILL.md` files plus a `.py` resource whose contents would raise if executed. Assert import succeeds as inert content, no execution occurs, recommendations are generated, explicit assignment activates chosen targets only, and resolved skills are visible only to those targets.

- [ ] **Step 2: Run focused verification**

```bash
pytest tests/staff_core/test_skill_archive_import.py tests/staff_core/test_skill_training_assignments.py tests/staff_core/test_sherman_control_plane.py tests/staff_core/test_sherman_frontend_contract.py -q
```

Expected: all PASS.

- [ ] **Step 3: Run full quality gate**

```bash
python -m compileall -q src tests
ruff check src tests
pytest tests/staff_core -q
```

Expected: zero failures/errors.

- [ ] **Step 4: Verify branch CI**

Require the repository CI workflow for the branch head to complete successfully. If CI differs from local verification, treat CI as authoritative and fix the discrepancy before PR review.

- [ ] **Step 5: Commit final test hardening if required**

```bash
git add tests/staff_core
git commit -m "test: verify Sherman skill training end to end"
```

- [ ] **Step 6: Open PR for review**

Open a PR from `feat/sherman-skill-training` to `main` summarizing architecture, security controls, tests, and any explicit v1 limitation such as unavailable native RAR/7z decoding. Do not merge without user approval.
