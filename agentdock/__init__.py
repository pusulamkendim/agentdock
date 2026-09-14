"""Public AgentDock API.

The implementation lives in focused modules.  This file intentionally only
imports those modules and re-exports the supported public names; it never
mutates another module's globals or forwards monkeypatches implicitly.
"""

import shutil
import sqlite3
import time

from . import (
    api,
    app,
    codex,
    config,
    db,
    git_ops,
    handoffs,
    integration,
    mission,
    mission_context,
    orchestrator,
    platform,
    preflight,
    schemas,
    tasks,
    timeline,
    version,
)
from .api import Handler
from .app import initialize_database, main, initialize_database as init_db
from .codex import (
    engine_status,
    interrupt_app_server,
    quota_status,
    run_codex,
    run_codex_app_server,
    run_orchestrator,
    steer_app_server,
    terminate_process,
    validate_runtime_config,
)
from .config import (
    APP_SERVER_CONTROLS,
    APP_SERVER_CONTROLS_LOCK,
    ATTACHMENT_ROOT,
    DB,
    DEFAULT_ORCHESTRATOR,
    DEFAULT_ORCHESTRATOR_EFFORT,
    DEFAULT_ORCHESTRATOR_TIER,
    DEFAULT_WORKER,
    DEFAULT_WORKER_EFFORT,
    DEFAULT_WORKER_TIER,
    HOST,
    LEGACY_DB,
    LOCAL_IGNORE_RULES,
    MAX_PARALLEL_HARD,
    MISSION_ROOT,
    MISSION_DECISIONS,
    NO_TASK_DECISIONS,
    ORCHESTRATOR_ACTIONS,
    ORCHESTRATOR_PURPOSES,
    PORT,
    RECOVERY_DEFAULTS,
    RUNNERS,
    RUNNERS_LOCK,
    STATE_ROOT,
    STATIC,
    VALID_TIERS,
    WORKTREE_ROOT,
    choose_workspace_folder,
    mission_dir,
    normalize_workspace_path,
    now,
    recovery_settings,
)
from .db import (
    claim_plan_run,
    claim_task_integration,
    create_agent_session,
    doctor_log_id,
    execute,
    finish_agent_session,
    latest_agent_session,
    log,
    one,
    plan_attachment_paths,
    record_codex_event,
    record_control_event,
    release_plan_run,
    rows,
    save_attachment,
    workspace_summary,
)
from .git_ops import (
    canonical_allowed_pattern,
    changed_git_paths,
    fingerprint_diff,
    initialize_git_repository,
    path_matches_allowed,
    repo_info,
    validate_read_workspace,
    validate_worker_changes,
    workspace_fingerprint,
    workspace_snapshot,
)
from .mission import (
    apply_plan,
    approve_plan,
    build_demo_plan,
    build_plan,
    create_agent_profile,
    create_plan_request,
    create_workspace_request,
    docs_payload,
    health_payload,
    integration_patch,
    live_payload,
    mission_config,
    plan_diff_payload,
    pause_plan,
    pause_task,
    quota_payload,
    ensure_workspace,
    migrate_legacy_orchestrator_state,
    replan_mission,
    reset_plan_for_retry,
    restart_as_new_mission,
    resume_plan,
    resume_task,
    run_plan,
    recover_orphaned_runs,
    start_plan,
    state_payload,
    stored_integration_context,
    workspace_browse,
)
from .mission_context import build_mission_context, mission_context_data, render_mission_context
from .handoffs import (
    consultation_payload,
    create_worker_consultation,
    format_contract_md,
    same_worker_resume_handoff,
    task_dependency_context,
    worker_resume_message,
)
from .integration import integrate_write_result, resolve_merge_conflict
from .orchestrator import (
    answer_consultation,
    answer_consultation_request,
    latest_orchestrator_session,
    orchestrator_log_id,
    plan_consultations,
    reconstruct_orchestrator_context,
    resolve_worker_consultation,
    resolve_worker_escalation,
    resolve_worker_failure,
    run_mission_orchestrator_turn,
    run_orchestrator_followup,
    start_orchestrator_followup,
    start_runtime_recovery,
    start_reconstruct,
)
from .preflight import PreflightBlocked, PreflightWaitingForUser, apply_preflight_action, run_preflight
from .schemas import (
    consultation_schema_path,
    deterministic_mission_title,
    normalize_consultation_result,
    normalize_planner_result,
    planner_schema_path,
    safe_json,
    validate_task_graph,
)
from .tasks import (
    cancel_task,
    configure_task,
    drain_queued_manual_followups,
    manual_followup_delivery_status,
    mark_read_result_done,
    open_task_terminal,
    queued_messages,
    run_demo_manual_followup,
    run_manual_followup,
    run_manual_followup_message,
    run_parallel_task,
    run_single_task,
    run_task_once,
    run_task_with_recovery,
    task_effort,
    task_model,
    task_tier,
)
from .platform import open_terminal_at
from .timeline import diff_payload, events_payload, latest_log_segment, logs_payload, messages_payload, mission_usage, task_diff, timeline_for, write_mission_docs
from .version import VERSION


__all__ = [
    "api", "app", "codex", "config", "db", "git_ops", "handoffs", "integration", "mission", "mission_context", "orchestrator", "platform",
    "preflight", "schemas", "tasks", "timeline", "Handler", "main", "init_db",
    "run_codex", "run_codex_app_server", "run_orchestrator", "run_plan",
    "run_mission_orchestrator_turn", "run_task_once", "run_parallel_task",
    "run_manual_followup", "run_manual_followup_message", "run_single_task",
    "drain_queued_manual_followups", "claim_task_integration",
    "resolve_merge_conflict", "integrate_write_result",
    "manual_followup_delivery_status", "timeline_for", "task_diff", "validate_task_graph", "canonical_allowed_pattern",
    "PreflightBlocked", "PreflightWaitingForUser", "apply_preflight_action",
    "build_plan", "apply_plan", "pause_plan", "resume_plan", "pause_task", "resume_task",
    "replan_mission", "restart_as_new_mission", "mission_config", "answer_consultation",
    "build_mission_context", "mission_context_data", "render_mission_context",
    "recover_orphaned_runs", "migrate_legacy_orchestrator_state", "rows", "one", "execute", "log", "now", "safe_json",
    "VERSION",
    "STATE_ROOT", "DB", "WORKTREE_ROOT", "MISSION_ROOT", "ATTACHMENT_ROOT", "LEGACY_DB",
    "APP_SERVER_CONTROLS", "APP_SERVER_CONTROLS_LOCK", "RUNNERS", "RUNNERS_LOCK",
    "RECOVERY_DEFAULTS", "STATIC", "HOST", "PORT", "shutil", "sqlite3", "time",
]
