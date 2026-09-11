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
    orchestrator,
    preflight,
    schemas,
    tasks,
    timeline,
)
from .api import Handler, manual_followup_delivery_status, open_terminal_at, run_manual_followup_message, run_single_task
from .app import main
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
    create_agent_session,
    doctor_log_id,
    ensure_workspace,
    execute,
    finish_agent_session,
    init_db,
    latest_agent_session,
    log,
    one,
    plan_attachment_paths,
    record_codex_event,
    record_control_event,
    recover_orphaned_runs,
    release_plan_run,
    rows,
    save_attachment,
    workspace_summary,
)
from .git_ops import (
    changed_git_paths,
    fingerprint_diff,
    path_matches_allowed,
    repo_info,
    validate_read_workspace,
    validate_worker_changes,
    workspace_fingerprint,
    workspace_snapshot,
)
from .mission import (
    apply_plan,
    build_demo_plan,
    build_plan,
    integration_patch,
    mission_config,
    pause_plan,
    pause_task,
    replan_mission,
    reset_plan_for_retry,
    restart_as_new_mission,
    resume_plan,
    resume_task,
    run_plan,
    stored_integration_context,
)
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
    latest_orchestrator_session,
    orchestrator_log_id,
    plan_consultations,
    reconstruct_orchestrator_context,
    resolve_worker_consultation,
    resolve_worker_escalation,
    resolve_worker_failure,
    run_mission_orchestrator_turn,
    run_orchestrator_followup,
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
    mark_read_result_done,
    queued_messages,
    run_demo_manual_followup,
    run_manual_followup,
    run_parallel_task,
    run_task_once,
    run_task_with_recovery,
    task_effort,
    task_model,
    task_tier,
)
from .timeline import latest_log_segment, mission_usage, task_diff, timeline_for, write_mission_docs


__all__ = [
    "api", "app", "codex", "config", "db", "git_ops", "handoffs", "integration", "mission", "orchestrator",
    "preflight", "schemas", "tasks", "timeline", "Handler", "main", "init_db",
    "run_codex", "run_codex_app_server", "run_orchestrator", "run_plan",
    "run_mission_orchestrator_turn", "run_task_once", "run_parallel_task",
    "run_manual_followup", "run_manual_followup_message", "run_single_task",
    "resolve_merge_conflict", "integrate_write_result",
    "manual_followup_delivery_status", "timeline_for", "task_diff", "validate_task_graph",
    "PreflightBlocked", "PreflightWaitingForUser", "apply_preflight_action",
    "build_plan", "apply_plan", "pause_plan", "resume_plan", "pause_task", "resume_task",
    "replan_mission", "restart_as_new_mission", "mission_config", "answer_consultation",
    "recover_orphaned_runs", "rows", "one", "execute", "log", "now", "safe_json",
    "STATE_ROOT", "DB", "WORKTREE_ROOT", "MISSION_ROOT", "ATTACHMENT_ROOT", "LEGACY_DB",
    "APP_SERVER_CONTROLS", "APP_SERVER_CONTROLS_LOCK", "RUNNERS", "RUNNERS_LOCK",
    "RECOVERY_DEFAULTS", "STATIC", "HOST", "PORT", "shutil", "sqlite3", "time",
]
