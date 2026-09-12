from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from nemotron.staff.adapters.capability_hmac import HMACCapabilityAuthority
from nemotron.staff.adapters.nemotron_planner import NemotronPlannerConfig, NemotronPlanningAdapter
from nemotron.staff.adapters.nemotron_worker import NemotronWorkerConfig, NemotronWorkerReasoningAdapter
from nemotron.staff.adapters.sqlite_control import (
    SQLiteAuditLog,
    SQLiteControlStore,
    SQLiteOrganizationRepository,
    SQLiteStaffRepository,
)
from nemotron.staff.adapters.sqlite_core import SQLiteTaskRepository
from nemotron.staff.adapters.sqlite_gateway import (
    SQLiteGatewayStore,
    SQLiteIdempotencyRepository,
    SQLiteToolIntentRepository,
)
from nemotron.staff.adapters.sqlite_runtime import (
    SQLiteGoalRepository,
    SQLiteMemoryRepository,
    SQLitePlanRepository,
    SQLiteRuntimeStore,
)
from nemotron.staff.adapters.sqlite_worker import SQLiteWorkerQueue
from nemotron.staff.adapters.tools import (
    BrowserToolAdapter,
    BrowserToolConfig,
    FilesToolAdapter,
    FilesToolConfig,
    GitHubToolAdapter,
    GitHubToolConfig,
    OdooToolAdapter,
    OdooToolConfig,
    SMTPEmailToolAdapter,
    SMTPToolConfig,
    browser_tool_definition,
    email_tool_definition,
    files_tool_definition,
    github_tool_definition,
    odoo_tool_definition,
)
from nemotron.staff.application.goals import CreateGoal
from nemotron.staff.application.memory import ReadVisibleMemory, WriteMemory
from nemotron.staff.application.planning import AcceptPlan, BuildPlanProposal
from nemotron.staff.application.runtime_ports import PlanningPort
from nemotron.staff.application.tool_gateway import ExecuteToolTask, InMemoryToolRegistry, PrepareToolExecution
from nemotron.staff.application.use_cases import ApproveTask, VerifyTask
from nemotron.staff.application.worker import StaffWorkerEngine
from nemotron.staff.application.worker_ports import WorkerReasoningPort
from nemotron.staff.domain import GovernancePolicy

from .backup import SQLiteBackupManager
from .config import ControlPlaneConfig
from .queries import ControlPlaneQueries
from .readiness import NemotronReadinessProbe


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UUIDGenerator:
    def new_id(self) -> str:
        return uuid4().hex


@dataclass(slots=True)
class ProductionRuntime:
    config: ControlPlaneConfig
    clock: UtcClock
    ids: UUIDGenerator
    policy: GovernancePolicy
    control_store: SQLiteControlStore
    staff: SQLiteStaffRepository
    organizations: SQLiteOrganizationRepository
    audit: SQLiteAuditLog
    tasks: SQLiteTaskRepository
    runtime_store: SQLiteRuntimeStore
    memories: SQLiteMemoryRepository
    goals: SQLiteGoalRepository
    plans: SQLitePlanRepository
    worker_queue: SQLiteWorkerQueue
    gateway_store: SQLiteGatewayStore
    intents: SQLiteToolIntentRepository
    idempotency: SQLiteIdempotencyRepository
    capabilities: HMACCapabilityAuthority
    tools: InMemoryToolRegistry
    planner: PlanningPort
    reasoner: WorkerReasoningPort
    create_goal: CreateGoal
    write_memory: WriteMemory
    read_memory: ReadVisibleMemory
    build_plan: BuildPlanProposal
    accept_plan: AcceptPlan
    worker: StaffWorkerEngine
    prepare_tool_execution: PrepareToolExecution
    approve_task: ApproveTask
    execute_tool_task: ExecuteToolTask
    verify_task: VerifyTask
    queries: ControlPlaneQueries
    backups: SQLiteBackupManager
    nemotron_readiness: NemotronReadinessProbe
    registered_tools: tuple[str, ...]

    def health(self) -> dict[str, object]:
        database_ok = False
        try:
            database_ok = self.control_store.check()
            with sqlite3.connect(self.config.database_path, timeout=5) as db:
                database_ok = database_ok and db.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            database_ok = False
        files_ok = self.config.files_root.exists() and os.access(self.config.files_root, os.R_OK | os.W_OK)
        backups_ok = self.config.backup_dir.exists() and os.access(self.config.backup_dir, os.R_OK | os.W_OK)
        mount = self.config.volume_mount_path
        persistent_volume = bool(mount and mount.exists() and self.config.data_dir.is_relative_to(mount))
        ready = database_ok and files_ok and backups_ok and "files" in self.registered_tools
        return {
            "status": "ready" if ready else "degraded",
            "ready": ready,
            "database": database_ok,
            "files_root": files_ok,
            "backup_storage": backups_ok,
            "persistent_volume": persistent_volume,
            "nemotron": {
                "model": self.config.nemotron_model,
                "api_key_configured": bool(self.config.nemotron_api_key),
            },
            "registered_tools": list(self.registered_tools),
        }

    def readiness(self, *, force: bool = False) -> dict[str, object]:
        local = self.health()
        probe = self.nemotron_readiness.check(force=force)
        ready = bool(local["ready"]) and probe.ready
        return {
            "status": "ready" if ready else "degraded",
            "ready": ready,
            "local": local,
            "nemotron": {
                "ready": probe.ready,
                "checked_at": probe.checked_at,
                "latency_ms": probe.latency_ms,
                "error": probe.error,
            },
        }


def build_production_runtime(
    config: ControlPlaneConfig,
    *,
    planner: PlanningPort | None = None,
    reasoner: WorkerReasoningPort | None = None,
) -> ProductionRuntime:
    config.prepare_paths()
    clock = UtcClock()
    ids = UUIDGenerator()
    policy = GovernancePolicy.conservative()
    db_path = config.database_path

    control_store = SQLiteControlStore(db_path)
    staff = SQLiteStaffRepository(control_store)
    organizations = SQLiteOrganizationRepository(control_store)
    audit = SQLiteAuditLog(control_store)
    tasks = SQLiteTaskRepository(db_path)

    runtime_store = SQLiteRuntimeStore(db_path)
    memories = SQLiteMemoryRepository(runtime_store)
    goals = SQLiteGoalRepository(runtime_store)
    plans = SQLitePlanRepository(runtime_store)
    worker_queue = SQLiteWorkerQueue(runtime_store)

    gateway_store = SQLiteGatewayStore(db_path)
    intents = SQLiteToolIntentRepository(gateway_store)
    idempotency = SQLiteIdempotencyRepository(gateway_store)
    capabilities = HMACCapabilityAuthority(config.capability_secret)
    tools = InMemoryToolRegistry()
    registered: list[str] = []

    files_adapter = FilesToolAdapter(FilesToolConfig(config.files_root), capabilities)
    tools.register(files_tool_definition(), files_adapter)
    registered.append("files")

    if config.github_allowed_repositories:
        github_read_only = config.github_read_only or not bool(config.github_token)
        tools.register(
            github_tool_definition(read_only=github_read_only),
            GitHubToolAdapter(
                GitHubToolConfig(
                    token=config.github_token,
                    allowed_repositories=config.github_allowed_repositories,
                    read_only=github_read_only,
                ),
                capabilities,
            ),
        )
        registered.append("github")

    if config.smtp_host and config.smtp_from_address:
        tools.register(
            email_tool_definition(),
            SMTPEmailToolAdapter(
                SMTPToolConfig(
                    host=config.smtp_host,
                    port=config.smtp_port,
                    sender=config.smtp_from_address,
                    username=config.smtp_username,
                    password=config.smtp_password,
                    allowed_recipient_domains=config.smtp_allowed_domains,
                ),
                capabilities,
            ),
        )
        registered.append("email")

    if config.odoo_base_url and config.odoo_database and config.odoo_uid and config.odoo_api_key:
        tools.register(
            odoo_tool_definition(),
            OdooToolAdapter(
                OdooToolConfig(
                    base_url=config.odoo_base_url,
                    database=config.odoo_database,
                    uid=config.odoo_uid,
                    api_key=config.odoo_api_key,
                    allowed_models=config.odoo_allowed_models,
                ),
                capabilities,
            ),
        )
        registered.append("odoo")

    if config.browser_allowed_hosts:
        tools.register(
            browser_tool_definition(),
            BrowserToolAdapter(BrowserToolConfig(config.browser_allowed_hosts), capabilities),
        )
        registered.append("browser")

    actual_planner = planner or NemotronPlanningAdapter(
        NemotronPlannerConfig(
            base_url=config.nemotron_base_url,
            model=config.nemotron_model,
            api_key=config.nemotron_api_key,
        )
    )
    actual_reasoner = reasoner or NemotronWorkerReasoningAdapter(
        NemotronWorkerConfig(
            base_url=config.nemotron_base_url,
            model=config.nemotron_model,
            api_key=config.nemotron_api_key,
        )
    )

    read_memory = ReadVisibleMemory(memories, staff, organizations)
    create_goal = CreateGoal(goals, staff, ids, clock, audit)
    write_memory = WriteMemory(memories, staff, organizations, ids, clock, audit)
    build_plan = BuildPlanProposal(
        goals,
        plans,
        actual_planner,
        staff,
        organizations,
        read_memory,
        audit,
        clock,
    )
    accept_plan = AcceptPlan(plans, staff, organizations, ids, clock, audit)
    worker = StaffWorkerEngine(
        queue=worker_queue,
        tasks=tasks,
        staff=staff,
        organizations=organizations,
        goals=goals,
        plans=plans,
        memories=read_memory,
        reasoner=actual_reasoner,
        policy=policy,
        clock=clock,
        audit=audit,
    )
    prepare_tool_execution = PrepareToolExecution(tasks, staff, intents, tools, clock, audit)
    approve_task = ApproveTask(tasks, staff, clock, audit)
    execute_tool_task = ExecuteToolTask(
        tasks,
        staff,
        intents,
        tools,
        capabilities,
        idempotency,
        clock,
        audit,
    )
    verify_task = VerifyTask(tasks, staff, policy, clock, audit)
    queries = ControlPlaneQueries(tasks, intents, idempotency, audit)
    backups = SQLiteBackupManager(db_path, config.backup_dir, retention=config.backup_retention)
    nemotron_readiness = NemotronReadinessProbe(
        base_url=config.nemotron_base_url,
        model=config.nemotron_model,
        api_key=config.nemotron_api_key,
        timeout_seconds=config.nemotron_readiness_timeout_seconds,
        ttl_seconds=config.nemotron_readiness_ttl_seconds,
    )

    return ProductionRuntime(
        config=config,
        clock=clock,
        ids=ids,
        policy=policy,
        control_store=control_store,
        staff=staff,
        organizations=organizations,
        audit=audit,
        tasks=tasks,
        runtime_store=runtime_store,
        memories=memories,
        goals=goals,
        plans=plans,
        worker_queue=worker_queue,
        gateway_store=gateway_store,
        intents=intents,
        idempotency=idempotency,
        capabilities=capabilities,
        tools=tools,
        planner=actual_planner,
        reasoner=actual_reasoner,
        create_goal=create_goal,
        write_memory=write_memory,
        read_memory=read_memory,
        build_plan=build_plan,
        accept_plan=accept_plan,
        worker=worker,
        prepare_tool_execution=prepare_tool_execution,
        approve_task=approve_task,
        execute_tool_task=execute_tool_task,
        verify_task=verify_task,
        queries=queries,
        backups=backups,
        nemotron_readiness=nemotron_readiness,
        registered_tools=tuple(registered),
    )
