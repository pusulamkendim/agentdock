#!/usr/bin/env python3
import concurrent.futures
import base64
import hashlib
import mimetypes
import json
import os
import re
import select
import queue
import signal
import shutil
import shlex
import sys
import sqlite3
import subprocess
import threading
import time
import uuid
import webbrowser
import fnmatch
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HOST = "127.0.0.1"
PORT = int(os.environ.get("AGENTDOCK_PORT", "8765"))
STATE_ROOT = Path(os.environ.get("AGENTDOCK_STATE_ROOT", str(Path.home() / ".agentdock"))).expanduser()
DB = Path(os.environ.get("AGENTDOCK_DB", str(STATE_ROOT / "agentdock.sqlite3"))).expanduser()
LEGACY_DB = ROOT / "agentdock.sqlite3"
WORKTREE_ROOT = STATE_ROOT / "worktrees"
MISSION_ROOT = STATE_ROOT / "missions"
ATTACHMENT_ROOT = STATE_ROOT / "attachments"

DB_LOCK = threading.RLock()
RUNNERS = {}
RUNNERS_LOCK = threading.RLock()
APP_SERVER_CONTROLS = {}
APP_SERVER_CONTROLS_LOCK = threading.RLock()
ACTIVE_PLAN_RUNS = set()
ACTIVE_PLAN_RUNS_LOCK = threading.RLock()
ENGINE_STATUS_CACHE = {"ts": 0.0, "value": None}
ENGINE_STATUS_LOCK = threading.RLock()
MODEL_CATALOG_CACHE = {"ts": 0.0, "value": []}
MODEL_CATALOG_LOCK = threading.RLock()
QUOTA_CACHE = {"ts": 0.0, "value": {"status": "idle", "available": False}, "refreshing": False}
QUOTA_LOCK = threading.RLock()
ORCHESTRATOR_TURN_LOCKS = {}
ORCHESTRATOR_TURN_LOCKS_LOCK = threading.RLock()

DEFAULT_ORCHESTRATOR = "gpt-5.6-sol"
DEFAULT_WORKER = "gpt-5.6-luna"
DEFAULT_ORCHESTRATOR_EFFORT = "high"
DEFAULT_WORKER_EFFORT = "medium"
DEFAULT_ORCHESTRATOR_TIER = "default"
DEFAULT_WORKER_TIER = "default"
MAX_PARALLEL_HARD = 8
VALID_TIERS = {"default", "fast"}
MISSION_DECISIONS = {
    "already_satisfied",
    "answer_only",
    "needs_user_input",
    "blocked",
    "execute",
}
NO_TASK_DECISIONS = {"already_satisfied", "answer_only", "needs_user_input", "blocked"}
ORCHESTRATOR_PURPOSES = {
    "initial_disposition",
    "user_answer",
    "worker_consultation",
    "failure_recovery",
    "contract_revision",
    "checkpoint_summary",
    "final_synthesis",
    "manual_message",
    "reconstruct",
}
ORCHESTRATOR_ACTIONS = {"answer_worker", "revise_contract", "ask_user", "block_mission"}
CODEX_TRANSPORT = os.environ.get("AGENTDOCK_CODEX_TRANSPORT", "exec").strip().lower()
RECOVERY_DEFAULTS = {
    # Preflight is intentionally observational. These keys remain in the
    # persisted policy for backwards compatibility, but are never executed
    # implicitly; the corresponding repair must be an explicit UI action.
    "auto_clean_generated": False,
    "auto_repair_ignores": False,
    "auto_retry_transient": True,
    "auto_remove_stale_worktrees": False,
    "auto_resolve_git_locks": False,
    "unknown_local_changes": "ask",
    "merge_conflicts": "orchestrator",
    "destructive_operations": "never",
}
SAFE_GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SAFE_GENERATED_FILES = {".DS_Store", "Thumbs.db", ".coverage", ".eslintcache"}
LOCAL_IGNORE_RULES = [
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", ".DS_Store", ".coverage", ".eslintcache",
]
TRANSIENT_ERROR_PATTERNS = [
    r"timed? out", r"timeout", r"connection reset", r"connection refused",
    r"temporary failure", r"temporarily unavailable", r"service unavailable",
    r"internal server error", r"bad gateway", r"gateway timeout", r"stream.*disconnect",
    r"transport.*error", r"network.*error", r"broken pipe", r"server busy",
]
MODEL_EFFORTS = {
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max"},
    "auto-best": {"low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-sol": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-terra": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-luna": {"none", "low", "medium", "high", "xhigh", "max"},
}

PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "decision": {
            "type": "string",
            "enum": ["already_satisfied", "answer_only", "needs_user_input", "blocked", "execute"],
        },
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 32},
        "final_response": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 12},
        "tasks": {
            "type": "array",
            "minItems": 0,
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "mode": {"type": "string", "enum": ["read", "write"]},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "contract": {
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "context": {"type": "string"},
                            "scope": {
                                "type": "object",
                                "properties": {
                                    "in_scope": {"type": "array", "items": {"type": "string"}},
                                    "out_of_scope": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["in_scope", "out_of_scope"],
                                "additionalProperties": False,
                            },
                            "allowed_paths": {"type": "array", "items": {"type": "string"}},
                            "required_inputs": {"type": "array", "items": {"type": "string"}},
                            "implementation_steps": {"type": "array", "items": {"type": "string"}},
                            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                            "verification_commands": {"type": "array", "items": {"type": "string"}},
                            "expected_output": {"type": "array", "items": {"type": "string"}},
                            "escalation_conditions": {"type": "array", "items": {"type": "string"}},
                            "decision_policy": {"type": "string"},
                        },
                        "required": [
                            "objective", "context", "scope", "allowed_paths", "required_inputs",
                            "implementation_steps", "acceptance_criteria", "verification_commands",
                            "expected_output", "escalation_conditions", "decision_policy",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["title", "agent_id", "mode", "depends_on", "contract"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decision", "reason", "evidence", "final_response", "questions", "tasks"],
    "additionalProperties": False,
}

CONSULTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["answer_worker", "revise_contract", "ask_user", "block_mission"],
        },
        "reason": {"type": "string"},
        "worker_message": {"type": "string"},
        "revised_contract": {"type": "object", "additionalProperties": True},
        "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
    },
    "required": ["action", "reason", "worker_message", "revised_contract", "questions", "evidence"],
    "additionalProperties": False,
}
# Structured-output providers require every object to declare its property
# policy. Reuse the planner's complete task contract and allow null when the
# response is an answer-only or user-question action.
CONSULTATION_SCHEMA["properties"]["revised_contract"] = {
    "anyOf": [
        json.loads(json.dumps(PLANNER_SCHEMA["properties"]["tasks"]["items"]["properties"]["contract"])),
        {"type": "object", "maxProperties": 0, "additionalProperties": False},
        {"type": "null"},
    ]
}


def planner_schema_path():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATE_ROOT / "planner.schema.json"
    expected = json.dumps(PLANNER_SCHEMA, ensure_ascii=False, indent=2) + "\n"
    if not path.exists() or path.read_text(errors="replace") != expected:
        path.write_text(expected)
    return path


def consultation_schema_path():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATE_ROOT / "orchestrator.consultation.schema.json"
    expected = json.dumps(CONSULTATION_SCHEMA, ensure_ascii=False, indent=2) + "\n"
    if not path.exists() or path.read_text(errors="replace") != expected:
        path.write_text(expected)
    return path

def validate_runtime_config(model, effort, tier, label):
    allowed = None
    if model:
        for item in discover_model_catalog():
            if item.get("slug") == model:
                allowed = set(item.get("reasoning_levels") or [])
                break
    if allowed is None:
        allowed = MODEL_EFFORTS.get(model)
    if allowed and effort not in allowed:
        raise ValueError(f"{label}: {model} için reasoning effort '{effort}' geçerli değil")
    if tier not in VALID_TIERS:
        raise ValueError(f"{label}: speed/service tier '{tier}' geçerli değil")


def now():
    return int(time.time())


def db():
    con = sqlite3.connect(DB, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def column_names(table):
    con = db()
    out = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    con.close()
    return out


def ensure_column(table, name, ddl):
    if name not in column_names(table):
        con = db()
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        con.commit()
        con.close()


def init_db():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    MISSION_ROOT.mkdir(parents=True, exist_ok=True)
    ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
    if not DB.exists() and LEGACY_DB.exists() and LEGACY_DB.resolve() != DB.resolve():
        shutil.copy2(LEGACY_DB, DB)
        print(f"Migrated legacy AgentDock database to {DB}")
    con = db()
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
          engine TEXT NOT NULL DEFAULT 'codex', model TEXT,
          mode TEXT NOT NULL DEFAULT 'read', created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plans(
          id TEXT PRIMARY KEY, goal TEXT NOT NULL, workspace TEXT NOT NULL,
          planner_engine TEXT NOT NULL, status TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          started_at INTEGER, finished_at INTEGER,
          apply_status TEXT NOT NULL DEFAULT '', apply_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, seq INTEGER NOT NULL,
          title TEXT NOT NULL, instructions TEXT NOT NULL,
          agent_id TEXT, mode TEXT NOT NULL DEFAULT 'read',
          depends_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
          output TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
          started_at INTEGER, finished_at INTEGER,
          FOREIGN KEY(plan_id) REFERENCES plans(id)
        );
        CREATE TABLE IF NOT EXISTS logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          ts INTEGER NOT NULL, stream TEXT NOT NULL, line TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspaces(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, repo_path TEXT NOT NULL UNIQUE,
          default_branch TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
          last_opened_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_sessions(
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL DEFAULT 'worker', thread_id TEXT NOT NULL DEFAULT '',
          turn_id TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL DEFAULT '',
          service_tier TEXT NOT NULL DEFAULT 'default', mode TEXT NOT NULL DEFAULT 'read',
          cwd TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'running',
          started_at INTEGER NOT NULL, finished_at INTEGER, final_response TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS agent_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          plan_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
          ts INTEGER NOT NULL, event_type TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS agent_events_session_idx ON agent_events(session_id,id);
        CREATE TABLE IF NOT EXISTS attachments(
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
          name TEXT NOT NULL, mime TEXT NOT NULL DEFAULT 'application/octet-stream',
          path TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_messages(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, plan_id TEXT NOT NULL DEFAULT '',
          ts INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '', attachments_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS task_messages_task_idx ON task_messages(task_id,ts);
        CREATE TABLE IF NOT EXISTS orchestrator_turns(
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '',
          turn_id TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued', context_json TEXT NOT NULL DEFAULT '{}',
          response_text TEXT NOT NULL DEFAULT '', response_json TEXT NOT NULL DEFAULT '{}',
          usage_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
          started_at INTEGER, finished_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS orchestrator_turns_plan_idx ON orchestrator_turns(plan_id,created_at,id);
        CREATE TABLE IF NOT EXISTS consultations(
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'queued', question TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '[]',
          options_json TEXT NOT NULL DEFAULT '[]', orchestrator_response_json TEXT NOT NULL DEFAULT '{}',
          user_questions_json TEXT NOT NULL DEFAULT '[]', user_answer_json TEXT NOT NULL DEFAULT '{}',
          worker_thread_id TEXT NOT NULL DEFAULT '', orchestrator_thread_id TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL, resolved_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS consultations_plan_idx ON consultations(plan_id,created_at,id);
        """
    )
    con.commit()
    con.close()

    # Lightweight migrations for v1 databases.
    for name, ddl in [
        ("orchestrator_model", "TEXT NOT NULL DEFAULT 'gpt-5.6-sol'"),
        ("orchestrator_used", "TEXT NOT NULL DEFAULT ''"),
        ("orchestrator_effort", "TEXT NOT NULL DEFAULT 'high'"),
        ("orchestrator_tier", "TEXT NOT NULL DEFAULT 'default'"),
        ("worker_model", "TEXT NOT NULL DEFAULT 'gpt-5.6-luna'"),
        ("worker_effort", "TEXT NOT NULL DEFAULT 'medium'"),
        ("worker_tier", "TEXT NOT NULL DEFAULT 'default'"),
        ("max_parallel", "INTEGER NOT NULL DEFAULT 4"),
        ("base_commit", "TEXT NOT NULL DEFAULT ''"),
        ("integration_workspace", "TEXT NOT NULL DEFAULT ''"),
        ("summary", "TEXT NOT NULL DEFAULT ''"),
        ("error", "TEXT NOT NULL DEFAULT ''"),
        ("applied", "INTEGER NOT NULL DEFAULT 0"),
        ("mission_dir", "TEXT NOT NULL DEFAULT ''"),
        ("usage_start_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("usage_end_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("preflight_status", "TEXT NOT NULL DEFAULT ''"),
        ("preflight_json", "TEXT NOT NULL DEFAULT '{}'") ,
        ("recovery_json", "TEXT NOT NULL DEFAULT '{}'") ,
        ("recovery_count", "INTEGER NOT NULL DEFAULT 0"),
        ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
        ("automation_mode", "TEXT NOT NULL DEFAULT 'auto'"),
        ("attachments_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("paused", "INTEGER NOT NULL DEFAULT 0"),
        ("demo_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("approved_at", "INTEGER"),
        ("approval_note", "TEXT NOT NULL DEFAULT ''"),
        ("started_at", "INTEGER"),
        ("finished_at", "INTEGER"),
        ("apply_status", "TEXT NOT NULL DEFAULT ''"),
        ("apply_error", "TEXT NOT NULL DEFAULT ''"),
        ("decision", "TEXT NOT NULL DEFAULT ''"),
        ("decision_reason", "TEXT NOT NULL DEFAULT ''"),
        ("evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("questions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("final_response", "TEXT NOT NULL DEFAULT ''"),
        ("workspace_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("replan_note", "TEXT NOT NULL DEFAULT ''"),
        ("orchestrator_thread_id", "TEXT NOT NULL DEFAULT ''"),
        ("orchestrator_generation", "INTEGER NOT NULL DEFAULT 1"),
        ("orchestrator_turn_status", "TEXT NOT NULL DEFAULT ''"),
        ("orchestrator_last_turn_id", "TEXT NOT NULL DEFAULT ''"),
        ("orchestrator_last_error", "TEXT NOT NULL DEFAULT ''"),
        ("pending_question_id", "TEXT NOT NULL DEFAULT ''"),
        ("pending_question_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("legacy_orchestrator_status", "TEXT NOT NULL DEFAULT ''"),
        ("workspace_choice_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("restart_recovery_pending", "INTEGER NOT NULL DEFAULT 0"),
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("pause_reason", "TEXT NOT NULL DEFAULT ''"),
        ("pause_requested_at", "INTEGER"),
        ("resume_count", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        ensure_column("plans", name, ddl)
    for name, ddl in [
        ("reasoning_effort", "TEXT NOT NULL DEFAULT ''"),
        ("service_tier", "TEXT NOT NULL DEFAULT ''"),
    ]:
        ensure_column("agents", name, ddl)
    for name, ddl in [
        ("workspace", "TEXT NOT NULL DEFAULT ''"),
        ("branch", "TEXT NOT NULL DEFAULT ''"),
        ("commit_hash", "TEXT NOT NULL DEFAULT ''"),
        ("integration_status", "TEXT NOT NULL DEFAULT ''"),
        ("contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("escalation_count", "INTEGER NOT NULL DEFAULT 0"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_failure_kind", "TEXT NOT NULL DEFAULT ''"),
        ("repair_count", "INTEGER NOT NULL DEFAULT 0"),
        ("worker_thread_id", "TEXT NOT NULL DEFAULT ''"),
        ("worker_resume_message", "TEXT NOT NULL DEFAULT ''"),
        ("waiting_reason", "TEXT NOT NULL DEFAULT ''"),
        ("consultation_id", "TEXT NOT NULL DEFAULT ''"),
        ("baseline_fingerprint_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("model_override", "TEXT NOT NULL DEFAULT ''"),
        ("reasoning_effort_override", "TEXT NOT NULL DEFAULT ''"),
        ("service_tier_override", "TEXT NOT NULL DEFAULT ''"),
        ("pause_reason", "TEXT NOT NULL DEFAULT ''"),
    ]:
        ensure_column("tasks", name, ddl)
    ensure_column("agent_sessions", "turn_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column("orchestrator_turns", "usage_json", "TEXT NOT NULL DEFAULT '{}'")

    con = db()
    cur = con.cursor()
    count = cur.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"]
    if count == 0:
        defaults = [
            ("architect", "Architect", "Analyze architecture, constraints, dependencies and risks. Return focused findings that unblock implementation.", "codex", "", "read"),
            ("coder", "Coder", "Implement focused code changes. Keep diffs minimal, run relevant checks, and report changed files and risks.", "codex", "", "write"),
            ("reviewer", "Reviewer", "Independently review the integrated result for regressions, missing edge cases, unsafe assumptions and incomplete verification.", "codex", "", "read"),
            ("researcher", "Researcher", "Investigate a focused question using project context and any safe tools exposed by the engine. Return evidence and concise conclusions.", "codex", "", "read"),
            ("tester", "Tester", "Design and run the smallest meaningful verification needed for the assigned change. Report exact commands and failures.", "codex", "", "read"),
        ]
        for row in defaults:
            cur.execute("INSERT INTO agents(id,name,role,engine,model,mode,created_at) VALUES(?,?,?,?,?,?,?)", (*row, now()))
    con.commit()
    con.close()

    planner_schema_path()
    consultation_schema_path()
    recover_orphaned_runs()

    # v0.7: materialize existing repository paths as first-class workspaces.
    con = db()
    existing = con.execute("SELECT id,workspace,workspace_id FROM plans ORDER BY created_at").fetchall()
    for pl in existing:
        repo_path = str(Path(pl["workspace"]).expanduser().resolve()) if pl["workspace"] else ""
        if not repo_path:
            continue
        ws = con.execute("SELECT id FROM workspaces WHERE repo_path=?", (repo_path,)).fetchone()
        if ws:
            wid = ws["id"]
        else:
            wid = str(uuid.uuid4())[:8]
            name = Path(repo_path).name or repo_path
            info = repo_info(repo_path) if Path(repo_path).exists() else {"branch":""}
            con.execute("INSERT INTO workspaces(id,name,repo_path,default_branch,created_at,last_opened_at) VALUES(?,?,?,?,?,?)",
                        (wid,name,repo_path,info.get("branch") or "",now(),now()))
        if not pl["workspace_id"]:
            con.execute("UPDATE plans SET workspace_id=? WHERE id=?", (wid,pl["id"]))
    con.commit()
    con.close()
    migrate_legacy_orchestrator_state()


def recover_orphaned_runs():
    """Move work interrupted by a server restart into an explicit retry state."""
    interrupted_at = now()
    with DB_LOCK:
        con = db()
        plans = con.execute(
            "SELECT id FROM plans WHERE status IN ('preflight','running','pausing','resuming')"
        ).fetchall()
        # A process can stop after an answer is durably recorded but before the
        # same orchestrator turn resolves it. Requeue that consultation while
        # preserving the user's answer for the next attempt.
        con.execute(
            "UPDATE consultations SET status='queued',orchestrator_response_json='{}',resolved_at=NULL WHERE status='resolving'"
        )
        if not plans:
            con.commit()
            con.close()
            return 0
        plan_ids = [r["id"] for r in plans]
        placeholders = ",".join("?" for _ in plan_ids)
        message = "AgentDock restarted while this mission was running; review and retry."
        con.execute(
            f"UPDATE plans SET status='attention', error=?, finished_at=?, apply_error=? "
            f"WHERE id IN ({placeholders})",
            (message, interrupted_at, message, *plan_ids),
        )
        con.execute(
            f"UPDATE plans SET restart_recovery_pending=1 WHERE id IN ({placeholders})",
            plan_ids,
        )
        con.execute(
            f"UPDATE tasks SET status='attention', error=?, finished_at=? "
            f"WHERE plan_id IN ({placeholders}) AND status IN ('running','pausing','resuming')",
            (message, interrupted_at, *plan_ids),
        )
        con.execute(
            f"UPDATE agent_sessions SET status='interrupted', finished_at=? "
            f"WHERE plan_id IN ({placeholders}) AND status='running'",
            (interrupted_at, *plan_ids),
        )
        con.execute(
            f"UPDATE plans SET orchestrator_turn_status=?,orchestrator_last_error=? "
            f"WHERE id IN ({placeholders}) AND orchestrator_turn_status='running'",
            ("attention", message, *plan_ids),
        )
        con.execute(
            f"UPDATE orchestrator_turns SET status=?,error=?,finished_at=? "
            f"WHERE plan_id IN ({placeholders}) AND status='running'",
            ("failed", message, interrupted_at, *plan_ids),
        )
        con.commit()
        con.close()
    for plan_id in plan_ids:
        log(orchestrator_log_id(plan_id), "supervisor", message)
        write_mission_docs(plan_id)
    return len(plan_ids)


def migrate_legacy_orchestrator_state():
    """Bind old missions to an existing thread without silently creating one."""
    plans = rows("SELECT * FROM plans ORDER BY created_at")
    for plan in plans:
        sessions = rows(
            """SELECT thread_id,kind,started_at FROM agent_sessions
               WHERE plan_id=? AND kind LIKE '%orchestrator%' AND thread_id!=''
               ORDER BY started_at,id""",
            (plan["id"],),
        )
        distinct = []
        for session in sessions:
            if session["thread_id"] not in distinct:
                distinct.append(session["thread_id"])
        current = str(plan.get("orchestrator_thread_id") or "")
        legacy = str(plan.get("legacy_orchestrator_status") or "")
        if current and legacy:
            continue
        if distinct:
            preferred = next(
                (s["thread_id"] for s in sessions if s.get("kind") == "orchestrator"),
                distinct[0],
            )
            if len(distinct) == 1:
                status = "adopted"
                message = "Legacy mission: existing orchestrator thread adopted."
            else:
                status = "reconciliation_required"
                message = (
                    "Legacy mission had multiple orchestrator threads; the earliest planner "
                    "thread was adopted and history reconciliation is required."
                )
            execute(
                """UPDATE plans SET orchestrator_thread_id=?,orchestrator_generation=?,
                   legacy_orchestrator_status=?,orchestrator_last_error=? WHERE id=?""",
                (current or preferred, int(plan.get("orchestrator_generation") or 1), status, message, plan["id"]),
            )
            log(orchestrator_log_id(plan["id"]), "supervisor", message)
            write_mission_docs(plan["id"])
        elif not current and not legacy and plan.get("status") not in ("done", "cancelled"):
            message = "Legacy mission: unified orchestrator session must be reconstructed explicitly."
            execute(
                "UPDATE plans SET legacy_orchestrator_status=?,orchestrator_last_error=? WHERE id=?",
                ("reconstruct_required", message, plan["id"]),
            )
            log(orchestrator_log_id(plan["id"]), "supervisor", message)
            write_mission_docs(plan["id"])


def rows(sql, args=()):
    con = db()
    out = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return out


def one(sql, args=()):
    con = db()
    r = con.execute(sql, args).fetchone()
    con.close()
    return dict(r) if r else None


def execute(sql, args=()):
    with DB_LOCK:
        con = db()
        con.execute(sql, args)
        con.commit()
        con.close()


def log(task_id, stream, line):
    if task_id:
        execute(
            "INSERT INTO logs(task_id,ts,stream,line) VALUES(?,?,?,?)",
            (task_id, now(), stream, str(line)[:12000]),
        )


def claim_plan_run(plan_id):
    """Prevent duplicate starts caused by double clicks or concurrent clients."""
    with ACTIVE_PLAN_RUNS_LOCK:
        if plan_id in ACTIVE_PLAN_RUNS:
            return False
        ACTIVE_PLAN_RUNS.add(plan_id)
        return True


def release_plan_run(plan_id):
    with ACTIVE_PLAN_RUNS_LOCK:
        ACTIVE_PLAN_RUNS.discard(plan_id)


def terminate_process(proc):
    """Terminate a Codex process and its shell descendants on macOS/Linux."""
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass


def plan_is_paused(plan_id):
    plan = one("SELECT status,paused FROM plans WHERE id=?", (plan_id,)) or {}
    return plan.get("status") in ("paused", "pausing") or int(plan.get("paused") or 0) == 1


def normalize_workspace_path(repo_path):
    """Resolve the path forms people naturally paste on macOS.

    Accepted examples:
      /Users/name/project
      ~/project
      project                  -> ~/project when it exists
      name/project             -> /Users/name/project when that exists
      Users/name/project       -> /Users/name/project
    """
    raw = str(repo_path or "").strip()
    if not raw:
        raise ValueError("Repository klasörü seçilmedi")

    home = Path.home()
    candidates = []
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        candidates.append(expanded)
    else:
        # A very common Finder/Terminal paste omits the leading /Users/.
        parts = expanded.parts
        if parts and parts[0] == home.name:
            candidates.append(home.parent / expanded)
        if parts and parts[0] == "Users":
            candidates.append(Path("/") / expanded)
        candidates.append(home / expanded)
        candidates.append(Path.cwd() / expanded)

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate.absolute()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_dir():
            return resolved

    # Return the most useful normalized path in the error so the UI can tell
    # the user exactly what was checked instead of showing a generic alert.
    best = candidates[0].resolve() if candidates else expanded.resolve()
    raise ValueError(f"Repository klasörü bulunamadı: {best}")


def choose_workspace_folder():
    """Open the native macOS folder chooser from the local AgentDock server."""
    if os.uname().sysname != "Darwin":
        raise RuntimeError("Folder picker şu anda yalnızca macOS'ta kullanılabilir")
    script = 'POSIX path of (choose folder with prompt "Choose a Git repository for AgentDock")'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        if "User canceled" in err or "(-128)" in err:
            return None
        raise RuntimeError(err or "Folder picker açılamadı")
    value = result.stdout.strip().rstrip("/")
    return str(Path(value).expanduser().resolve()) if value else None


def ensure_workspace(repo_path, name=None):
    path = normalize_workspace_path(repo_path)
    canonical = str(path)
    current = one("SELECT * FROM workspaces WHERE repo_path=?", (canonical,))
    info = repo_info(canonical)
    if current:
        execute("UPDATE workspaces SET last_opened_at=?, default_branch=CASE WHEN ?<>'' THEN ? ELSE default_branch END WHERE id=?",
                (now(), info.get("branch") or "", info.get("branch") or "", current["id"]))
        if name and name.strip() and name.strip() != current.get("name"):
            execute("UPDATE workspaces SET name=? WHERE id=?", (name.strip(), current["id"]))
        return one("SELECT * FROM workspaces WHERE id=?", (current["id"],))
    wid = str(uuid.uuid4())[:8]
    execute("INSERT INTO workspaces(id,name,repo_path,default_branch,created_at,last_opened_at) VALUES(?,?,?,?,?,?)",
            (wid, (name or path.name or canonical).strip(), canonical, info.get("branch") or "", now(), now()))
    return one("SELECT * FROM workspaces WHERE id=?", (wid,))


def workspace_summary(workspace):
    wid = workspace["id"]
    plans = rows("SELECT id,goal,title,status,created_at,error,max_parallel,worker_model,orchestrator_model FROM plans WHERE workspace_id=? ORDER BY created_at DESC", (wid,))
    running = queued = attention = done = 0
    for pl in plans:
        stats = one("""SELECT
            SUM(CASE WHEN status IN ('running','resuming','pausing') THEN 1 ELSE 0 END) running,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) queued,
            SUM(CASE WHEN status IN ('failed','blocked','cancelled','attention','paused_by_user') THEN 1 ELSE 0 END) issues,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done
            FROM tasks WHERE plan_id=?""", (pl["id"],)) or {}
        pl["task_stats"] = {k: int(stats.get(k) or 0) for k in ("running","queued","issues","done")}
        running += pl["task_stats"]["running"]
        queued += pl["task_stats"]["queued"]
        attention += pl["task_stats"]["issues"] + (1 if pl["status"] == "attention" else 0)
        done += pl["task_stats"]["done"]
    out = dict(workspace)
    out["plans"] = plans
    out["stats"] = {"running": running, "queued": queued, "attention": attention, "done": done}
    return out


def create_agent_session(plan_id, task_id, kind, model, effort, tier, mode, cwd):
    sid = str(uuid.uuid4())
    execute("""INSERT INTO agent_sessions(id,plan_id,task_id,kind,model,reasoning_effort,service_tier,mode,cwd,status,started_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, plan_id or "", task_id or "", kind, model or "", effort or "", tier or "default", mode, str(cwd), "running", now()))
    return sid


def record_codex_event(session_id, task_id, plan_id, raw_line):
    try:
        obj = json.loads(raw_line)
    except Exception:
        return None
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    typ = str(obj.get("type") or obj.get("method") or "event")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else params.get("item") if isinstance(params.get("item"), dict) else {}
    itype = str(item.get("type") or "")
    execute("INSERT INTO agent_events(session_id,plan_id,task_id,ts,event_type,item_type,payload_json) VALUES(?,?,?,?,?,?,?)",
            (session_id, plan_id or "", task_id or "", now(), typ, itype, json.dumps(obj, ensure_ascii=False)[:120000]))
    if typ in ("thread.started", "thread/started"):
        thread = obj.get("thread") if isinstance(obj.get("thread"), dict) else params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = str(obj.get("thread_id") or thread.get("id") or "")
        if thread_id:
            execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (thread_id, session_id))
            # Bind the mission as soon as the first planner event exposes the
            # thread. Never replace an already-bound thread from an ordinary
            # resume; the gateway performs the hard mismatch check after the
            # turn completes.
            if plan_id and str(task_id or "") == orchestrator_log_id(plan_id):
                execute(
                    "UPDATE plans SET orchestrator_thread_id=? WHERE id=? AND (orchestrator_thread_id='' OR orchestrator_thread_id=?)",
                    (thread_id, plan_id, thread_id),
                )
    if typ in ("turn.started", "turn/started"):
        turn = obj.get("turn") if isinstance(obj.get("turn"), dict) else params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = str(obj.get("turn_id") or params.get("turnId") or turn.get("id") or "")
        if turn_id:
            execute("UPDATE agent_sessions SET turn_id=? WHERE id=?", (turn_id, session_id))
    return obj


def finish_agent_session(session_id, status, final_response=""):
    execute("UPDATE agent_sessions SET status=?, finished_at=?, final_response=? WHERE id=?",
            (status, now(), str(final_response or "")[-50000:], session_id))


def latest_agent_session(task_id):
    return one("SELECT * FROM agent_sessions WHERE task_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,))


def latest_log_segment(items, marker):
    """Keep only the newest logical run while preserving the full raw log."""
    start = 0
    for index, item in enumerate(items or []):
        if marker in str(item.get("line") or ""):
            start = index
    return (items or [])[start:]


def record_control_event(plan_id, event_type, payload=None, task_id=""):
    """Persist a human-readable control-plane event alongside raw Codex events."""
    session = latest_agent_session(orchestrator_log_id(plan_id))
    if not session:
        return
    obj = {"type": event_type, "plan_id": plan_id}
    if isinstance(payload, dict):
        obj.update(payload)
    record_codex_event(session["id"], task_id or orchestrator_log_id(plan_id), plan_id, json.dumps(obj, ensure_ascii=False))


def plan_attachment_paths(plan):
    vals = safe_json((plan or {}).get("attachments_json"), []) if "safe_json" in globals() else []
    if isinstance(vals, list):
        return [str(x) for x in vals if x and Path(str(x)).is_file()]
    return []


def attachment_dir(plan_id):
    path = ATTACHMENT_ROOT / (plan_id or "drafts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_attachment(plan_id, name, mime, data_b64, task_id=""):
    mime = str(mime or "").lower().strip()
    if not mime.startswith("image/"):
        raise ValueError("Yalnızca image/* attachment kabul ediliyor")
    raw = base64.b64decode(data_b64, validate=True)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("Attachment 20 MB sınırını aşıyor")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "attachment").name)[:120] or "attachment"
    ext = Path(safe_name).suffix
    if not ext:
        guessed = mimetypes.guess_extension(mime or "") or ""
        safe_name += guessed
    aid = str(uuid.uuid4())[:12]
    path = attachment_dir(plan_id) / f"{aid}-{safe_name}"
    path.write_bytes(raw)
    execute("INSERT INTO attachments(id,plan_id,task_id,name,mime,path,created_at) VALUES(?,?,?,?,?,?,?)",
            (aid, plan_id or "", task_id or "", safe_name, mime, str(path), now()))
    return {"id": aid, "name": safe_name, "mime": mime, "path": str(path), "size": len(raw)}


def shell(args, cwd=None, input_text=None, check=True, timeout=None, env=None):
    p = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"exit {p.returncode}")[-12000:])
    return p


def git(cwd, *args, check=True, input_text=None):
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    return shell([exe, "-C", str(cwd), *args], input_text=input_text, check=check)


def git_read(cwd, *args, check=True, input_text=None):
    """Run an observational Git command without optional index refresh locks."""
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return shell([exe, "-C", str(cwd), *args], input_text=input_text, check=check, env=env)


def repo_info(workspace):
    workspace = Path(workspace).expanduser().resolve()
    try:
        root = Path(git_read(workspace, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        head = git_read(root, "rev-parse", "HEAD").stdout.strip()
        rel = workspace.relative_to(root)
        dirty = bool(git_read(root, "status", "--porcelain").stdout.strip())
        branch = git_read(root, "branch", "--show-current", check=False).stdout.strip()
        return {"is_git": True, "root": str(root), "head": head, "rel": str(rel), "dirty": dirty, "branch": branch}
    except Exception:
        return {"is_git": False, "root": str(workspace), "head": "", "rel": ".", "dirty": False}



def recovery_settings(plan):
    configured = safe_json((plan or {}).get("recovery_json"), {}) if "safe_json" in globals() else {}
    out = dict(RECOVERY_DEFAULTS)
    if isinstance(configured, dict):
        out.update({k: configured[k] for k in RECOVERY_DEFAULTS if k in configured})
    return out


def doctor_log_id(plan_id):
    return f"doctor:{plan_id}"


def _git_dir(repo_root, common=False):
    arg = "--git-common-dir" if common else "--git-dir"
    raw = git_read(repo_root, "rev-parse", arg).stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(repo_root) / p).resolve()
    return p


def git_status_entries(repo_root):
    out = git_read(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = []
    parts = [x for x in out.split("\0") if x]
    i = 0
    while i < len(parts):
        rec = parts[i]
        if len(rec) < 3:
            i += 1
            continue
        xy = rec[:2]
        path = rec[3:] if rec[2:3] == " " else rec[2:].lstrip()
        entries.append({"xy": xy, "path": path})
        # Rename/copy records include a second NUL path. It is never auto-cleaned,
        # so consume the extra field only to keep parsing aligned.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i + 1 < len(parts):
                entries[-1]["old_path"] = parts[i + 1]
                i += 1
        i += 1
    return entries


def git_remote_details(repo_root):
    """Return remote fetch/push URLs without changing Git configuration."""
    names = [x.strip() for x in git_read(repo_root, "remote", check=False).stdout.splitlines() if x.strip()]
    remotes = []
    for name in names:
        fetch = [x.strip() for x in git_read(repo_root, "config", "--get-all", f"remote.{name}.url", check=False).stdout.splitlines() if x.strip()]
        push = [x.strip() for x in git_read(repo_root, "config", "--get-all", f"remote.{name}.pushurl", check=False).stdout.splitlines() if x.strip()]
        remotes.append({"name": name, "fetch_urls": fetch, "push_urls": push or fetch})
    return remotes


def workspace_snapshot(workspace):
    """Build a bounded, deterministic, read-only snapshot for the planner."""
    resolved = Path(workspace).expanduser().resolve()
    info = repo_info(resolved)
    snapshot = {
        "workspace": str(resolved),
        "classification": "NOT_GIT",
        "repo_root": str(resolved),
        "branch": "",
        "head": "",
        "upstream": "",
        "remotes": [],
        "tracked_changes": [],
        "untracked_files": [],
        "working_tree_clean": True,
        "write_safety": {
            "read_only_inspection": "available",
            "isolated_write": "unavailable",
            "requires_user_resolution": False,
        },
        "captured_at": now(),
    }
    if not info.get("is_git"):
        snapshot["write_safety"]["requires_user_resolution"] = True
        snapshot["write_safety"]["reason"] = "A write task needs a Git repository for AgentDock isolation."
        return snapshot

    root = Path(info["root"])
    entries = git_status_entries(root)
    tracked = [f"{e['xy']} {e['path']}" for e in entries if e["xy"] != "??"]
    untracked = [e["path"] for e in entries if e["xy"] == "??"]
    upstream = git_read(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).stdout.strip()
    remotes = git_remote_details(root)
    snapshot.update({
        "classification": "GIT_WITH_REMOTE" if remotes else "LOCAL_GIT",
        "repo_root": str(root),
        "branch": info.get("branch") or "",
        "head": info.get("head") or "",
        "workspace_rel": info.get("rel") or ".",
        "upstream": upstream,
        "remotes": remotes,
        "tracked_changes": tracked[:100],
        "untracked_files": untracked[:100],
        "working_tree_clean": not entries,
        "write_safety": {
            "read_only_inspection": "available",
            "isolated_write": "available",
            "requires_user_resolution": bool(entries),
            "reason": "Working tree changes need an explicit user choice before isolated writes." if entries else "Clean base is available for isolated writes.",
        },
    })
    return snapshot


FINGERPRINT_MAX_FILES = 2000
FINGERPRINT_MAX_HASH_BYTES = 64 * 1024 * 1024
FINGERPRINT_MAX_TOTAL_HASH_BYTES = 256 * 1024 * 1024
FINGERPRINT_SKIP_DIRS = {".git", ".agentdock", "node_modules", ".venv", "venv"}


def _is_agentdock_state_path(path):
    target = Path(path).expanduser().resolve()
    for base in (STATE_ROOT, MISSION_ROOT, WORKTREE_ROOT, ATTACHMENT_ROOT):
        try:
            target.relative_to(Path(base).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _sha256_file(path, max_bytes=None):
    digest = hashlib.sha256()
    remaining = max_bytes
    try:
        with Path(path).open("rb") as handle:
            while True:
                size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
                if size <= 0:
                    break
                chunk = handle.read(size)
                if not chunk:
                    break
                digest.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return ""


def _path_metadata(root, path, hash_budget):
    rel = str(Path(path).relative_to(root)).replace(os.sep, "/")
    try:
        stat = Path(path).lstat()
    except OSError:
        return {"path": rel, "missing": True}
    item = {
        "path": rel,
        "type": "symlink" if Path(path).is_symlink() else ("directory" if Path(path).is_dir() else "file"),
        "size": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }
    if item["type"] == "file" and item["size"] <= FINGERPRINT_MAX_HASH_BYTES and hash_budget[0] + item["size"] <= FINGERPRINT_MAX_TOTAL_HASH_BYTES:
        item["sha256"] = _sha256_file(path)
        hash_budget[0] += item["size"]
    elif item["type"] == "file":
        item["sha256"] = ""
        item["sha256_skipped"] = True
    return item


def _git_patch_hash(repo_root, *args):
    result = git_read(repo_root, "diff", *args, check=False)
    return hashlib.sha256((result.stdout or "").encode("utf-8", errors="replace")).hexdigest()


def workspace_fingerprint(workspace):
    """Capture the workspace state before/after a read worker without requiring a clean tree."""
    resolved = Path(workspace).expanduser().resolve()
    info = repo_info(resolved)
    fingerprint = {
        "workspace": str(resolved),
        "kind": "git" if info.get("is_git") else "filesystem",
        "captured_at": now(),
    }
    budget = [0]
    if info.get("is_git"):
        root = Path(info["root"])
        entries = [
            entry for entry in git_status_entries(root)
            if not _is_agentdock_state_path(root / entry.get("path", ""))
        ]
        manifest = []
        for entry in entries:
            if entry.get("xy") != "??":
                continue
            target = root / entry["path"]
            if target.exists() or target.is_symlink():
                manifest.append(_path_metadata(root, target, budget))
            else:
                manifest.append({"path": entry["path"], "missing": True})
        manifest.sort(key=lambda x: x.get("path", ""))
        fingerprint.update({
            "root": str(root),
            "head": info.get("head") or "",
            "branch": info.get("branch") or "",
            "staged_diff_hash": _git_patch_hash(root, "--cached", "--binary"),
            "unstaged_diff_hash": _git_patch_hash(root, "--binary"),
            "status_entries": sorted(
                [{k: v for k, v in entry.items() if k in ("xy", "path", "old_path")} for entry in entries],
                key=lambda x: (x.get("path", ""), x.get("xy", "")),
            ),
            "untracked_manifest": manifest[:FINGERPRINT_MAX_FILES],
        })
        return fingerprint

    manifest = []
    if resolved.is_dir():
        for current, dirs, files in os.walk(resolved, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs
                if d not in FINGERPRINT_SKIP_DIRS and not _is_agentdock_state_path(Path(current) / d)
            )
            files = sorted(files)
            for name in files:
                if len(manifest) >= FINGERPRINT_MAX_FILES:
                    break
                path = Path(current) / name
                if (path.is_symlink() or path.is_file()) and not _is_agentdock_state_path(path):
                    manifest.append(_path_metadata(resolved, path, budget))
            if len(manifest) >= FINGERPRINT_MAX_FILES:
                break
    fingerprint["manifest"] = sorted(manifest, key=lambda x: x.get("path", ""))
    return fingerprint


def _comparable_fingerprint(value):
    if not isinstance(value, dict):
        return value
    return {key: val for key, val in value.items() if key != "captured_at"}


def fingerprint_diff(before, after):
    """Return human-readable changed paths/fields between two fingerprints."""
    before = _comparable_fingerprint(before or {})
    after = _comparable_fingerprint(after or {})
    if before == after:
        return []
    changes = []
    for key in ("kind", "root", "head", "branch", "staged_diff_hash", "unstaged_diff_hash", "status_entries"):
        if before.get(key) != after.get(key):
            changes.append(key)
    before_manifest = {x.get("path"): x for x in (before.get("untracked_manifest") or before.get("manifest") or []) if isinstance(x, dict)}
    after_manifest = {x.get("path"): x for x in (after.get("untracked_manifest") or after.get("manifest") or []) if isinstance(x, dict)}
    for path in sorted(set(before_manifest) | set(after_manifest)):
        if before_manifest.get(path) != after_manifest.get(path):
            changes.append(path or "<unknown path>")
    if not changes:
        changes.append("workspace fingerprint")
    return changes[:100]


def safe_generated_target(repo_root, rel_path):
    root = Path(repo_root).resolve()
    rel = Path(rel_path)
    parts = rel.parts
    for idx, part in enumerate(parts):
        if part in SAFE_GENERATED_DIRS:
            return (root / Path(*parts[: idx + 1])).resolve()
    name = rel.name
    if name in SAFE_GENERATED_FILES or name.endswith((".pyc", ".pyo")) or name.startswith(".coverage."):
        return (root / rel).resolve()
    return None


def _safe_remove_path(repo_root, target):
    root = Path(repo_root).resolve()
    target = Path(target).resolve()
    if target == root or root not in target.parents:
        raise RuntimeError(f"Refusing cleanup outside repository: {target}")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def repair_local_ignore_rules(repo_root):
    common = _git_dir(repo_root, common=True)
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(errors="replace") if exclude.exists() else ""
    current = {line.strip() for line in existing.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    missing = [x for x in LOCAL_IGNORE_RULES if x not in current]
    if missing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a") as f:
            f.write(prefix + "\n# AgentDock self-healing generated-file rules\n")
            for rule in missing:
                f.write(rule + "\n")
    return missing


def _path_is_open(path):
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    r = shell([lsof, str(path)], check=False, timeout=3)
    return r.returncode == 0 and bool(r.stdout.strip())


def repair_known_git_locks(repo_root):
    gitdir = _git_dir(repo_root)
    repaired, blocked = [], []
    for name in ("index.lock", "HEAD.lock", "config.lock"):
        lock = gitdir / name
        if not lock.exists():
            continue
        age = max(0, time.time() - lock.stat().st_mtime)
        opened = _path_is_open(lock)
        # Only remove a lock when it is clearly stale and lsof proves no process owns it.
        if age >= 120 and opened is False:
            lock.unlink()
            repaired.append(str(lock))
        else:
            reason = "active" if opened else ("ownership unknown" if opened is None else "too recent")
            blocked.append(f"Git lock cannot be safely removed ({reason}): {lock}")
    return repaired, blocked


def is_transient_error(error):
    text = str(error or "").lower()
    # Quota exhaustion / auth failures should not be spam-retried.
    hard = ("usage limit", "weekly limit", "not logged in", "unauthorized", "forbidden", "insufficient quota")
    if any(x in text for x in hard):
        return False
    return any(re.search(p, text, re.I) for p in TRANSIENT_ERROR_PATTERNS)


def update_preflight(plan_id, status, report):
    execute(
        "UPDATE plans SET preflight_status=?, preflight_json=? WHERE id=?",
        (status, json.dumps(report, ensure_ascii=False), plan_id),
    )
    write_mission_docs(plan_id)


class PreflightWaitingForUser(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


class PreflightBlocked(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


def preflight_action_options(has_write, affected_paths, has_read=False):
    options = []
    if affected_paths:
        options.extend([
            {
                "id": "ignore",
                "label": "Ignore selected files locally",
                "description": "Adds only the selected paths to this repository's local .git/info/exclude.",
                "requires_paths": True,
            },
            {
                "id": "stage",
                "label": "Add selected files to Git",
                "description": "Stages the selected paths; AgentDock never commits them automatically.",
                "requires_paths": True,
            },
            {
                "id": "move",
                "label": "Move selected files to AgentDock safe area",
                "description": "Moves selected untracked files to the local AgentDock preservation area.",
                "requires_paths": True,
            },
        ])
    options.append({
        "id": "continue_read_only",
        "label": "Continue in read-only mode",
        "description": "Run only read tasks and leave write tasks paused.",
        "requires_paths": False,
        "disabled": not has_read,
    })
    options.append({
        "id": "verify_again",
        "label": "Run verification again",
        "description": "Repeat the read-only workspace checks after you resolve the reported condition.",
        "requires_paths": False,
    })
    options.append({
        "id": "cancel",
        "label": "Cancel mission",
        "description": "Stop this mission without changing the workspace.",
        "requires_paths": False,
    })
    return options


def run_preflight(plan, has_write, phase="execution"):
    plan_id = plan["id"]
    report = {
        "phase": phase,
        "status": "running",
        "read_only": True,
        "checks": [],
        "warnings": [],
        "repairs": [],
        "blockers": [],
        "affected_paths": [],
        "action_options": [],
        "started_at": now(),
        "finished_at": None,
    }
    log(doctor_log_id(plan_id), "supervisor", f"preflight started · phase={phase}")
    update_preflight(plan_id, "running", report)

    codex = shutil.which("codex")
    if codex:
        report["checks"].append("Codex CLI available")
        log(doctor_log_id(plan_id), "system", "✓ Codex CLI available")
    else:
        report["blockers"].append("Codex CLI PATH içinde bulunamadı")

    workspace = Path(plan["workspace"]).expanduser().resolve()
    snapshot = workspace_snapshot(workspace)
    report["snapshot"] = snapshot
    info = repo_info(workspace)
    task_modes = [t.get("mode") for t in rows("SELECT mode FROM tasks WHERE plan_id=?", (plan_id,))]
    has_read = any(mode == "read" for mode in task_modes)
    if has_write and not info["is_git"]:
        report["blockers"].append("Write execution needs a Git repository for isolated worktrees")
        report["action_options"] = preflight_action_options(has_write, [], has_read)
    elif info["is_git"]:
        repo_root = Path(info["root"])
        report["checks"].append(f"Git repository: {repo_root}")
        log(doctor_log_id(plan_id), "system", f"✓ Git repository {repo_root}")
        if snapshot.get("branch"):
            report["checks"].append(f"Branch: {snapshot['branch']}")
        if snapshot.get("upstream"):
            report["checks"].append(f"Upstream: {snapshot['upstream']}")
        if snapshot.get("remotes"):
            report["checks"].append("Remotes: " + ", ".join(x["name"] for x in snapshot["remotes"]))

        # These checks are deliberately read-only. Even stale worktree metadata
        # and Git locks are reported for an explicit user decision; they are
        # never pruned or removed by preflight.
        dry = git_read(repo_root, "worktree", "prune", "--dry-run", check=False).stdout.strip()
        if dry:
            report["warnings"].append("Stale Git worktree metadata is present; no automatic prune was performed.")
        else:
            report["checks"].append("No stale Git worktree metadata")

        gitdir = _git_dir(repo_root)
        for name in ("index.lock", "HEAD.lock", "config.lock"):
            lock = gitdir / name
            if not lock.exists():
                continue
            age = max(0, time.time() - lock.stat().st_mtime)
            opened = _path_is_open(lock)
            owner = "active" if opened else ("ownership unknown" if opened is None else f"{int(age)}s old")
            message = f"Git lock detected ({owner}): {lock}"
            if has_write:
                report["blockers"].append(message + "; write execution requires explicit resolution")
            else:
                report["warnings"].append(message + "; read-only execution will not remove or modify it")

        entries = git_status_entries(repo_root)
        tracked = [f"{e['xy']} {e['path']}" for e in entries if e["xy"] != "??"]
        unknown = [e["path"] for e in entries if e["xy"] == "??"]
        affected = [e["path"] for e in entries]
        report["affected_paths"] = affected[:100]
        accepted_paths = set(safe_json(plan.get("workspace_choice_json"), []))
        if has_write and affected:
            if phase == "execution" and set(affected).issubset(accepted_paths):
                report["warnings"].append("User explicitly accepted the current changed paths for isolated execution.")
                report["checks"].append("Working tree choice recorded; isolated writes will use a clean worktree base")
                log(doctor_log_id(plan_id), "system", "✓ explicit workspace choice recorded for current changes")
            else:
                report["blockers"].append("User changes detected; isolated write execution needs an explicit workspace choice: " + "; ".join(affected[:12]))
                report["action_options"] = preflight_action_options(has_write, affected, has_read)
        elif has_write:
            report["checks"].append("Working tree clean")
            log(doctor_log_id(plan_id), "system", "✓ working tree clean")
        elif affected:
            report["warnings"].append("Working tree has local changes; read-only inspection does not require a clean tree.")
            report["checks"].append("Read-only mission may inspect the existing working tree")
        else:
            report["checks"].append("Working tree clean")

    report["finished_at"] = now()
    if report["blockers"]:
        # Every blocked preflight must expose a safe next step even when the
        # problem is not tied to a selectable file (for example a missing
        # Codex binary or a Git lock).
        if not report["action_options"]:
            report["action_options"] = preflight_action_options(has_write, report["affected_paths"], has_read)
        if not codex:
            for option in report["action_options"]:
                if option.get("id") == "continue_read_only":
                    option["disabled"] = True
        hard_blocker = any("Git lock" in item or "Codex CLI" in item or "needs a Git repository" in item for item in report["blockers"])
        report["status"] = "blocked" if hard_blocker else "waiting_for_user"
        for b in report["blockers"]:
            log(doctor_log_id(plan_id), "stderr", b)
        update_preflight(plan_id, report["status"], report)
        record_control_event(plan_id, "agentdock.preflight", {"status": report["status"], "message": "Preflight needs an explicit user decision before execution."})
        message = "Preflight waiting for user: " if report["status"] == "waiting_for_user" else "Preflight blocked: "
        exc = PreflightWaitingForUser if report["status"] == "waiting_for_user" else PreflightBlocked
        raise exc(message + " | ".join(report["blockers"]), report)
    report["status"] = "ready"
    update_preflight(plan_id, "ready", report)
    record_control_event(plan_id, "agentdock.preflight", {"status": "ready", "message": "Read-only workspace checks complete."})
    log(doctor_log_id(plan_id), "supervisor", "preflight ready · read-only checks complete")
    return report


def selected_preflight_paths(repo_root, plan_id, selected):
    """Validate paths supplied by the explicit preflight action UI."""
    if not isinstance(selected, list) or not selected:
        raise ValueError("En az bir dosya seçilmelidir")
    root = Path(repo_root).resolve()
    current = {x["path"]: x for x in git_status_entries(root)}
    result = []
    for raw in selected[:100]:
        rel = str(raw or "").replace("\\", "/").lstrip("./")
        if not rel or rel not in current or rel.startswith("../") or "/../" in f"/{rel}":
            raise ValueError(f"Preflight dosya seçimi geçersiz: {raw}")
        target = (root / rel).resolve()
        if target == root or root not in target.parents:
            raise ValueError(f"Preflight dosya seçimi repository dışına çıkıyor: {raw}")
        result.append((rel, target, current[rel]))
    return result


def apply_preflight_action(plan_id, action, selected=None):
    """Apply one user-confirmed, narrow workspace action and optionally resume."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    report = safe_json(plan.get("preflight_json"), {})
    if plan.get("status") not in ("waiting_for_user", "blocked"):
        raise ValueError("Bu mission şu anda bir preflight kararı beklemiyor")
    action = str(action or "").strip()
    if action not in {"ignore", "stage", "move", "continue_read_only", "verify_again", "cancel"}:
        raise ValueError("Geçersiz preflight aksiyonu")

    if action == "cancel":
        execute("UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?", ("cancelled", "Mission cancelled by user", now(), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "mission cancelled by user during preflight")
        write_mission_docs(plan_id)
        return {"ok": True, "status": "cancelled"}

    info = repo_info(plan["workspace"])
    if not info.get("is_git"):
        raise ValueError("Bu aksiyon için workspace bir Git repository olmalı")
    root = Path(info["root"])
    selected_rows = selected_preflight_paths(root, plan_id, selected) if action in {"ignore", "stage", "move"} else []
    changed = []
    accepted_paths = set(safe_json(plan.get("workspace_choice_json"), []))
    if action == "ignore":
        common = _git_dir(root, common=True)
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(errors="replace") if exclude.exists() else ""
        lines = existing.splitlines()
        current = {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}
        rules = ["/" + rel for rel, _, _ in selected_rows]
        missing = [rule for rule in rules if rule not in current]
        if missing:
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a") as handle:
                handle.write(prefix + "\n# AgentDock explicit local ignore action\n")
                for rule in missing:
                    handle.write(rule + "\n")
            changed = missing
        log(orchestrator_log_id(plan_id), "supervisor", "user explicitly added local ignore rules: " + ", ".join(rules))
    elif action == "stage":
        git(root, "add", "--", *[rel for rel, _, _ in selected_rows])
        changed = [rel for rel, _, _ in selected_rows]
        accepted_paths.update(changed)
        execute(
            "UPDATE plans SET workspace_choice_json=? WHERE id=?",
            (json.dumps(sorted(accepted_paths), ensure_ascii=False), plan_id),
        )
        log(orchestrator_log_id(plan_id), "supervisor", "user explicitly staged files (no commit created): " + ", ".join(changed))
    elif action == "move":
        safe_root = ATTACHMENT_ROOT / plan_id / "preflight-preserved"
        moved = []
        for rel, target, entry in selected_rows:
            if entry.get("xy") != "??":
                raise ValueError(f"Yalnızca untracked dosyalar safe area'ya taşınabilir: {rel}")
            if not target.exists() and not target.is_symlink():
                raise ValueError(f"Dosya artık bulunamadı: {rel}")
            destination = safe_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"Safe area hedefi zaten var: {destination}")
            shutil.move(str(target), str(destination))
            moved.append({"path": rel, "preserved_at": str(destination)})
        changed = moved
        log(orchestrator_log_id(plan_id), "supervisor", "user explicitly moved files to AgentDock safe area: " + ", ".join(x["path"] for x in moved))
    elif action == "continue_read_only":
        tasks = rows("SELECT mode FROM tasks WHERE plan_id=?", (plan_id,))
        if not any(t.get("mode") == "read" for t in tasks):
            raise ValueError("Bu mission içinde read-only çalıştırılabilecek task yok")
        execute("UPDATE plans SET status=?,error=? WHERE id=?", ("approved", "", plan_id))
        if claim_plan_run(plan_id):
            threading.Thread(target=run_plan, args=(plan_id,), kwargs={"claimed": True, "read_only_only": True}, daemon=True).start()
        return {"ok": True, "status": "approved", "resuming": True, "read_only_only": True}

    refreshed = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    phase = report.get("phase") or "execution"
    has_write = any(t.get("mode") == "write" for t in rows("SELECT mode FROM tasks WHERE plan_id=?", (plan_id,)))
    try:
        latest_report = run_preflight(refreshed, has_write, phase=phase)
    except PreflightWaitingForUser as exc:
        return {"ok": True, "status": "waiting_for_user", "report": exc.report, "changed": changed}
    except PreflightBlocked as exc:
        return {"ok": True, "status": "blocked", "report": exc.report, "changed": changed}

    if phase == "execution":
        execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("approved", "", plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "preflight resolved by user; resuming mission automatically")
        if claim_plan_run(plan_id):
            threading.Thread(target=run_plan, args=(plan_id,), kwargs={"claimed": True}, daemon=True).start()
        return {"ok": True, "status": "approved", "resuming": True, "report": latest_report, "changed": changed}
    execute("UPDATE plans SET status=?,apply_status=?,apply_error=?,error=? WHERE id=?", ("awaiting_apply", "ready", "", "", plan_id))
    log(orchestrator_log_id(plan_id), "supervisor", "apply preflight resolved by user; diff is ready for review")
    write_mission_docs(plan_id)
    return {"ok": True, "status": "awaiting_apply", "report": latest_report, "changed": changed}


def reset_plan_for_retry(plan, preserve_completed=False):
    plan_id = plan["id"]
    log(
        orchestrator_log_id(plan_id),
        "supervisor",
        "retry requested; " + ("preserving completed task checkpoints" if preserve_completed else "resetting task runtime state while preserving contracts"),
    )
    info = repo_info(plan["workspace"])
    if info["is_git"]:
        repo_root = Path(info["root"])
        base_dir, integration_dir = plan_paths(plan_id)
        task_rows = rows("SELECT status,mode,integration_status,branch FROM tasks WHERE plan_id=?", (plan_id,))
        if preserve_completed:
            # Keep the integration worktree: it contains the commits from
            # completed write tasks. Only discard worktrees/branches belonging
            # to tasks that still need to run.
            if base_dir.exists():
                for child in list(base_dir.iterdir()):
                    if child == integration_dir:
                        continue
                    remove_worktree(repo_root, child)
            for task_row in task_rows:
                preserved = task_row.get("status") == "done" or (
                    task_row.get("status") == "executed" and (
                        task_row.get("mode") == "read"
                        or task_row.get("integration_status") in (
                            "integrated", "no_changes", "resolved_by_orchestrator", "read_complete"
                        )
                    )
                )
                if not preserved and task_row.get("branch"):
                    delete_branch(repo_root, task_row["branch"])
            git(repo_root, "worktree", "prune", check=False)
        else:
            remove_worktree(repo_root, integration_dir)
            # Remove any task worktrees registered under this plan.
            if base_dir.exists():
                for child in list(base_dir.iterdir()):
                    if child == integration_dir:
                        continue
                    remove_worktree(repo_root, child)
            for task_row in task_rows:
                if task_row.get("branch"):
                    delete_branch(repo_root, task_row["branch"])
            delete_branch(repo_root, f"agentdock/{plan_id}/integration")
            git(repo_root, "worktree", "prune", check=False)
            if base_dir.exists():
                shutil.rmtree(base_dir, ignore_errors=True)
    if preserve_completed:
        execute(
            """UPDATE tasks SET status='pending', output='', error='', started_at=NULL,
               finished_at=NULL, workspace='', branch='', commit_hash='', integration_status='',
               retry_count=0, last_failure_kind='', repair_count=0, waiting_reason='',
               consultation_id='', worker_resume_message='', baseline_fingerprint_json='{}'
               WHERE plan_id=? AND NOT (
                 status='done'
                 OR status IN ('waiting_for_orchestrator','waiting_for_user')
                 OR (status='executed' AND (
                   mode='read' OR integration_status IN ('integrated','no_changes','resolved_by_orchestrator','read_complete')
                 ))
               )""",
            (plan_id,),
        )
        execute(
            """UPDATE plans SET summary='', applied=0, error='', apply_status='', apply_error='',
               pending_question_id='', pending_question_json='{}', started_at=NULL,
               finished_at=NULL, restart_recovery_pending=0 WHERE id=?""",
            (plan_id,),
        )
    else:
        execute(
            """UPDATE tasks SET status='pending', output='', error='', started_at=NULL,
               finished_at=NULL, workspace='', branch='', commit_hash='', integration_status='',
               retry_count=0, last_failure_kind='', repair_count=0, waiting_reason='',
               consultation_id='', worker_resume_message='', baseline_fingerprint_json='{}' WHERE plan_id=?""",
            (plan_id,),
        )
        execute(
            """UPDATE plans SET base_commit='', integration_workspace='', summary='', applied=0,
               error='', apply_status='', apply_error='', pending_question_id='',
               pending_question_json='{}', started_at=NULL, finished_at=NULL,
               restart_recovery_pending=0 WHERE id=?""",
            (plan_id,),
        )
    write_mission_docs(plan_id)


def discover_model_catalog(force=False):
    """Read the bundled Codex model catalog without making a model call."""
    with MODEL_CATALOG_LOCK:
        cached = MODEL_CATALOG_CACHE.get("value") or []
        if cached and not force and (time.time() - MODEL_CATALOG_CACHE.get("ts", 0)) < 300:
            return list(cached)
    exe = shutil.which("codex")
    if not exe:
        return []
    try:
        result = shell([exe, "debug", "models", "--bundled"], check=False, timeout=10)
        payload = json.loads(result.stdout or "{}")
        catalog = []
        for item in payload.get("models") or []:
            if not isinstance(item, dict) or not item.get("slug"):
                continue
            catalog.append({
                "slug": item["slug"],
                "display_name": item.get("display_name") or item["slug"],
                "reasoning_levels": [
                    x.get("effort") for x in (item.get("supported_reasoning_levels") or [])
                    if isinstance(x, dict) and x.get("effort")
                ],
                "speed_tiers": [
                    x for x in (item.get("additional_speed_tiers") or []) if isinstance(x, str)
                ],
            })
    except Exception:
        catalog = []
    with MODEL_CATALOG_LOCK:
        MODEL_CATALOG_CACHE["ts"] = time.time()
        MODEL_CATALOG_CACHE["value"] = catalog
    return list(catalog)


def engine_status(force=False):
    # Live dashboard polls often; avoid spawning `codex` on every request.
    with ENGINE_STATUS_LOCK:
        cached = ENGINE_STATUS_CACHE.get("value")
        if cached is not None and not force and (time.time() - ENGINE_STATUS_CACHE.get("ts", 0)) < 12:
            return cached

    codex = shutil.which("codex")
    out = {
        "codex": {"installed": bool(codex), "path": codex, "version": "", "login": "unknown", "models": [], "app_server": bool(codex), "transport": CODEX_TRANSPORT},
        "git": {"installed": bool(shutil.which("git")), "path": shutil.which("git")},
    }
    if codex:
        try:
            out["codex"]["version"] = shell([codex, "--version"], check=False, timeout=3).stdout.strip()
        except Exception:
            pass
        out["codex"]["models"] = discover_model_catalog()
        try:
            r = shell([codex, "login", "status"], check=False, timeout=4)
            text = (r.stdout + "\n" + r.stderr).strip()
            if r.returncode == 0:
                out["codex"]["login"] = text or "signed in"
            elif text:
                out["codex"]["login"] = text[:300]
        except Exception:
            pass

    with ENGINE_STATUS_LOCK:
        ENGINE_STATUS_CACHE["ts"] = time.time()
        ENGINE_STATUS_CACHE["value"] = out
    return out



def _app_server_request(method, params=None, timeout=10):
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    proc = subprocess.Popen(
        [exe, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_buf = []
    def drain_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_buf.append(line)
        except Exception:
            pass
    threading.Thread(target=drain_stderr, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def read_until(request_id, deadline):
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
            except Exception:
                ready = [proc.stdout]
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result")
        detail = "".join(stderr_buf)[-2000:]
        raise RuntimeError(f"Codex app-server timeout for {method}. {detail}".strip())

    try:
        deadline = time.time() + timeout
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentdock", "title": "AgentDock", "version": "0.4.0"}, "capabilities": {"experimentalApi": True}}})
        read_until(1, deadline)
        send({"method": "initialized"})
        send({"id": 2, "method": method, **({"params": params} if params is not None else {})})
        return read_until(2, deadline)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try: proc.kill()
            except Exception: pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


def _normalize_rate_limits(result):
    result = result or {}
    by_id = result.get("rateLimitsByLimitId") or {}
    snap = by_id.get("codex") or result.get("rateLimits") or {}
    windows = {}
    for key in ("primary", "secondary"):
        bucket = snap.get(key)
        if not isinstance(bucket, dict):
            continue
        mins = bucket.get("windowDurationMins")
        if mins is None:
            continue
        item = {
            "used_percent": bucket.get("usedPercent"),
            "remaining_percent": None if bucket.get("usedPercent") is None else max(0, 100 - float(bucket.get("usedPercent"))),
            "window_minutes": mins,
            "resets_at": bucket.get("resetsAt"),
        }
        windows[str(mins)] = item
    return {
        "status": "ok",
        "available": True,
        "plan_type": snap.get("planType"),
        "five_hour": windows.get("300"),
        "weekly": windows.get("10080"),
        "windows": windows,
        "fetched_at": now(),
    }


def _refresh_quota_cache():
    try:
        value = _normalize_rate_limits(_app_server_request("account/rateLimits/read"))
    except Exception as e:
        value = {"status": "error", "available": False, "error": str(e), "fetched_at": now()}
    with QUOTA_LOCK:
        QUOTA_CACHE["ts"] = time.time()
        QUOTA_CACHE["value"] = value
        QUOTA_CACHE["refreshing"] = False


def quota_status(force=False, wait=False):
    with QUOTA_LOCK:
        fresh = (time.time() - QUOTA_CACHE.get("ts", 0)) < 45
        if fresh and not force:
            return dict(QUOTA_CACHE["value"])
        if not QUOTA_CACHE.get("refreshing"):
            QUOTA_CACHE["refreshing"] = True
            t = threading.Thread(target=_refresh_quota_cache, daemon=True)
            t.start()
        else:
            t = None
        current = dict(QUOTA_CACHE.get("value") or {})
        current["refreshing"] = True
    if wait:
        deadline = time.time() + 12
        while time.time() < deadline:
            with QUOTA_LOCK:
                if not QUOTA_CACHE.get("refreshing"):
                    return dict(QUOTA_CACHE["value"])
            time.sleep(0.05)
    return current


def orchestrator_log_id(plan_id):
    return f"orchestrator:{plan_id}"


def mission_dir(plan_id):
    return MISSION_ROOT / plan_id


def safe_json(text, default=None):
    try:
        return json.loads(text or "")
    except Exception:
        return {} if default is None else default


def consultation_attachment_paths(task_id):
    """Return still-present image attachments supplied for a task consultation."""
    if not task_id:
        return []
    paths = []
    consultation_rows = rows(
        "SELECT user_answer_json FROM consultations WHERE task_id=? ORDER BY created_at,id",
        (task_id,),
    )
    for item in consultation_rows:
        answer = safe_json(item.get("user_answer_json"), {})
        for raw in answer.get("attachments") or [] if isinstance(answer, dict) else []:
            path = Path(str(raw)).expanduser().resolve()
            if path.is_file() and str(path) not in paths:
                paths.append(str(path))
    return paths[:8]


def task_input_attachment_paths(plan, task):
    paths = []
    for raw in plan_attachment_paths(plan) + consultation_attachment_paths((task or {}).get("id")):
        value = str(Path(raw).expanduser().resolve())
        if value not in paths and Path(value).is_file():
            paths.append(value)
    return paths[:16]


def format_contract_md(contract):
    if not isinstance(contract, dict):
        contract = {}
    def bullets(values):
        vals = values or []
        if isinstance(vals, str): vals = [vals]
        return "\n".join(f"- {v}" for v in vals) or "- None specified"
    scope = contract.get("scope") or {}
    return f"""## Objective
{contract.get('objective') or 'Not specified'}

## Context
{contract.get('context') or 'Not specified'}

## In scope
{bullets(scope.get('in_scope'))}

## Out of scope
{bullets(scope.get('out_of_scope'))}

## Allowed paths
{bullets(contract.get('allowed_paths'))}

## Required inputs
{bullets(contract.get('required_inputs'))}

## Execution steps
{bullets(contract.get('implementation_steps'))}

## Acceptance criteria
{bullets(contract.get('acceptance_criteria'))}

## Verification
{bullets(contract.get('verification_commands'))}

## Expected output
{bullets(contract.get('expected_output'))}

## Escalate instead of deciding when
{bullets(contract.get('escalation_conditions'))}

## Decision policy
{contract.get('decision_policy') or 'Do not make architecture or scope decisions. Escalate ambiguity to the orchestrator.'}
"""


def quota_delta(start, end, key):
    a = (start or {}).get(key)
    b = (end or {}).get(key)
    if not a or not b:
        return None
    if a.get("resets_at") and b.get("resets_at") and a.get("resets_at") != b.get("resets_at"):
        return {"reset_during_mission": True}
    au, bu = a.get("used_percent"), b.get("used_percent")
    if au is None or bu is None:
        return None
    return {"used_percent_delta": round(float(bu) - float(au), 2), "reset_during_mission": False}


def mission_usage(plan, current=None):
    start = safe_json(plan.get("usage_start_json"), {})
    stored_end = safe_json(plan.get("usage_end_json"), {})
    ended = bool(stored_end and (stored_end.get("five_hour") or stored_end.get("weekly") or stored_end.get("status") == "error"))
    end = stored_end if ended else (current or {})
    return {
        "start": start,
        "end": end,
        "live": not ended,
        "five_hour_delta": quota_delta(start, end, "five_hour"),
        "weekly_delta": quota_delta(start, end, "weekly"),
    }


def write_mission_docs(plan_id):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        return
    task_rows = rows("SELECT t.*,a.name agent_name,a.model agent_model,a.reasoning_effort agent_effort,a.service_tier agent_tier FROM tasks t LEFT JOIN agents a ON a.id=t.agent_id WHERE t.plan_id=? ORDER BY t.seq", (plan_id,))
    d = mission_dir(plan_id)
    td = d / "tasks"
    td.mkdir(parents=True, exist_ok=True)
    if plan.get("mission_dir") != str(d):
        execute("UPDATE plans SET mission_dir=? WHERE id=?", (str(d), plan_id))
    usage = mission_usage(plan)
    recovery = recovery_settings(plan)
    preflight = safe_json(plan.get("preflight_json"), {})
    evidence = safe_json(plan.get("evidence_json"), [])
    questions = safe_json(plan.get("questions_json"), [])
    snapshot = safe_json(plan.get("workspace_snapshot_json"), {})
    pending_question = safe_json(plan.get("pending_question_json"), {})
    consultations = plan_consultations(plan_id)
    mission = f"""# AgentDock Mission {plan_id}

- Status: `{plan['status']}`
- Workspace: `{plan['workspace']}`
- Orchestrator: `{plan.get('orchestrator_used') or plan.get('orchestrator_model')}` · effort `{plan.get('orchestrator_effort')}` · speed `{plan.get('orchestrator_tier')}`
- Worker default: `{plan.get('worker_model')}` · effort `{plan.get('worker_effort')}` · speed `{plan.get('worker_tier')}`
- Max parallel: `{plan.get('max_parallel')}`
- Created: `{plan.get('created_at')}`
- Started: `{plan.get('started_at') or ''}`
- Finished: `{plan.get('finished_at') or ''}`
- Apply: `{plan.get('apply_status') or 'not applicable'}`
- Mission disposition: `{plan.get('decision') or 'not decided'}`
- Orchestrator thread: `{plan.get('orchestrator_thread_id') or 'not bound'}`
- Orchestrator generation: `{plan.get('orchestrator_generation') or 1}`
- Last turn: `{plan.get('orchestrator_last_turn_id') or ''}`
- Turn status: `{plan.get('orchestrator_turn_status') or ''}`
- Legacy state: `{plan.get('legacy_orchestrator_status') or 'none'}`

## Goal

{plan['goal']}

## Mission disposition

- Decision: `{plan.get('decision') or 'not decided'}`
- Reason: {plan.get('decision_reason') or 'Not decided yet.'}
- Final response: {plan.get('final_response') or '—'}
- Evidence: {', '.join(str(x) for x in evidence) or '—'}
- Questions: {', '.join(str(x) for x in questions) or '—'}

## Workspace snapshot

```json
{json.dumps(snapshot, ensure_ascii=False, indent=2)}
```

## Recovery policy

```json
{json.dumps(recovery, ensure_ascii=False, indent=2)}
```

## Orchestrator consultations

```json
{json.dumps(consultations, ensure_ascii=False, indent=2)}
```

## Pending user question

```json
{json.dumps(pending_question, ensure_ascii=False, indent=2)}
```

## Preflight

- Status: `{plan.get('preflight_status') or 'not run'}`

```json
{json.dumps(preflight, ensure_ascii=False, indent=2)}
```

## Usage snapshots

```json
{json.dumps(usage, ensure_ascii=False, indent=2)}
```
"""
    (d / "MISSION.md").write_text(mission)
    lines = ["# Execution Plan", "", f"Mission: `{plan_id}`", "", "| # | Task | Agent | Mode | Depends | Status |", "|---:|---|---|---|---|---|"]
    for t in task_rows:
        deps = [str(x+1) for x in safe_json(t.get("depends_json"), [])]
        lines.append(f"| {t['seq']+1:02d} | {t['title']} | {t.get('agent_name') or t.get('agent_id')} | {t['mode']} | {', '.join(deps) or '—'} | {t['status']} |")
    lines += ["", "The SQLite database is the runtime source of truth. These Markdown files are human-readable mirrors generated by AgentDock."]
    (d / "PLAN.md").write_text("\n".join(lines) + "\n")
    for t in task_rows:
        contract = safe_json(t.get("contract_json"), {})
        deps = [str(x+1) for x in safe_json(t.get("depends_json"), [])]
        content = f"""# TASK-{t['seq']+1:03d} — {t['title']}

- Status: `{t['status']}`
- Agent: `{t.get('agent_name') or t.get('agent_id')}`
- Runtime: `{(t.get('agent_model') or '').strip() or plan.get('worker_model')}` · effort `{(t.get('agent_effort') or '').strip() or plan.get('worker_effort')}` · speed `{(t.get('agent_tier') or '').strip() or plan.get('worker_tier')}`
- Mode: `{t['mode']}`
- Depends on: `{', '.join(deps) or 'none'}`
- Branch: `{t.get('branch') or ''}`
- Commit: `{t.get('commit_hash') or ''}`
- Automatic retries: `{t.get('retry_count') or 0}`
- Orchestrator repair attempts: `{t.get('repair_count') or 0}`
- Last failure kind: `{t.get('last_failure_kind') or ''}`
- Worker thread: `{t.get('worker_thread_id') or 'not bound'}`
- Waiting reason: `{t.get('waiting_reason') or ''}`
- Orchestrator handoff: `{t.get('worker_resume_message') or ''}`

{format_contract_md(contract)}

## Worker result

{t.get('output') or t.get('error') or 'Not run yet.'}
"""
        (td / f"TASK-{t['seq']+1:03d}.md").write_text(content)
    preflight_md = ["# Preflight & Recovery", "", f"Status: `{plan.get('preflight_status') or 'not run'}`", ""]
    for title, key in (("Checks", "checks"), ("Warnings", "warnings"), ("Repairs", "repairs"), ("Blockers", "blockers"), ("Action options", "action_options")):
        preflight_md += [f"## {title}", ""]
        values = preflight.get(key) or []
        preflight_md += [f"- {x}" for x in values] or ["- None"]
        preflight_md += [""]
    preflight_md += ["## Policy", "", "```json", json.dumps(recovery, ensure_ascii=False, indent=2), "```", ""]
    (d / "PREFLIGHT.md").write_text("\n".join(preflight_md))
    (d / "FINAL.md").write_text(f"# Final Synthesis\n\n{plan.get('summary') or 'Mission has not finished yet.'}\n")
    log_rows = rows("SELECT id,task_id,ts,stream,line FROM logs WHERE task_id IN (?,?) OR task_id IN (SELECT id FROM tasks WHERE plan_id=?) ORDER BY id", (orchestrator_log_id(plan_id), doctor_log_id(plan_id), plan_id))
    with (d / "events.jsonl").open("w") as f:
        for item in log_rows:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

def parse_codex_final(stdout):
    final = []

    def normalize(value):
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        item = obj.get("item") or params.get("item") or {}
        item_type = str(item.get("type") or "").replace("-", "_")
        if obj.get("type") == "item.completed" and item_type in ("agent_message", "agentMessage"):
            text = normalize(item.get("text") or item.get("message"))
            if text:
                final.append(text)
        if obj.get("method") == "item/completed" and item_type in ("agentMessage", "agent_message"):
            text = normalize(item.get("text") or item.get("message"))
            if text:
                final.append(text)
        if obj.get("type") in ("message.completed", "response.completed"):
            text = obj.get("text") or obj.get("message") or ""
            if isinstance(text, str) and text:
                final.append(text)
    return final[-1] if final else stdout[-16000:]


def pretty_codex_event(line):
    raw = line.rstrip("\n")
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    typ = obj.get("type") or obj.get("method") or "event"
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    item = obj.get("item") or params.get("item") or {}
    itype = str(item.get("type") or "")
    normalized_type = re.sub(r"(?<!^)(?=[A-Z])", "_", itype).replace("-", "_").lower()
    if normalized_type in ("agent_message", "message"):
        text = item.get("text") or item.get("message") or ""
        try:
            parsed = json.loads(text) if isinstance(text, str) else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and ("decision" in parsed or "tasks" in parsed):
            return "planner response captured · structured disposition parsed"
        return f"agent: {text}" if text else typ
    if normalized_type in ("command_execution", "command"):
        cmd = item.get("command") or item.get("cmd") or ""
        status = item.get("status") or ""
        return f"cmd {status}: {cmd}".strip()
    if normalized_type in ("reasoning", "analysis"):
        summary = item.get("summary")
        if isinstance(summary, list):
            summary_parts = []
            for part in summary:
                if isinstance(part, str):
                    summary_parts.append(part)
                elif isinstance(part, dict):
                    value = part.get("text") or part.get("summary") or part.get("content") or ""
                    if value:
                        summary_parts.append(str(value))
            summary = " ".join(summary_parts)
        text = item.get("text") or summary or ""
        return f"thinking: {text}" if text else typ
    if normalized_type == "file_change":
        changes = item.get("changes") or []
        detail = ", ".join(f"{c.get('kind','?')} {c.get('path','')}" for c in changes[:8])
        return f"files {item.get('status','')}: {detail}".strip()
    if normalized_type == "todo_list":
        todos = item.get("items") or []
        done = sum(1 for x in todos if x.get("completed"))
        return f"todo: {done}/{len(todos)} complete"
    if normalized_type == "web_search":
        return f"web search: {item.get('query','')}"
    if normalized_type in ("mcp_tool_call", "collab_tool_call"):
        return f"tool {item.get('status','')}: {item.get('tool') or item.get('server') or itype}"
    text = obj.get("text") or obj.get("message") or ""
    if isinstance(text, str) and text:
        return f"{typ}: {text}"
    if item:
        label = item.get("name") or item.get("title") or itype
        return f"{typ}: {label}" if label else typ
    return typ


def infer_plan_id(task_id):
    if not task_id:
        return ""
    if str(task_id).startswith("orchestrator:"):
        return str(task_id).split(":", 1)[1]
    task = one("SELECT plan_id FROM tasks WHERE id=?", (task_id,))
    return task.get("plan_id", "") if task else ""


def _app_server_sandbox(workspace, mode):
    if mode == "write":
        return {"type": "workspaceWrite", "writableRoots": [str(workspace)]}
    return {"type": "readOnly", "access": {"type": "fullAccess"}}


def app_server_input_items(prompt, images=None, task_id=None):
    items = []
    if str(prompt or "").strip():
        items.append({"type": "text", "text": str(prompt)})
    for raw in images or []:
        path = Path(str(raw)).expanduser().resolve()
        if path.is_file():
            items.append({"type": "localImage", "path": str(path)})
        else:
            log(task_id, "supervisor", f"App Server image attachment skipped because it no longer exists: {path}")
    return items or [{"type": "text", "text": "Continue the assigned task."}]


def run_codex_app_server(prompt, workspace, mode="read", model="", task_id=None, reasoning_effort="", service_tier="default",
                         images=None, resume_thread_id="", session_kind="worker", output_schema=""):
    """Run one Codex turn through the line-delimited App Server protocol.

    The client is intentionally short-lived per turn. This gives AgentDock the
    App Server event vocabulary and resumable thread ids without introducing a
    daemon lifecycle dependency; resume_thread_id keeps the conversation
    continuous. The legacy exec transport remains available as the default.
    """
    workspace = str(Path(workspace).expanduser().resolve())
    if not Path(workspace).is_dir():
        raise RuntimeError(f"Workspace bulunamadı: {workspace}")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    plan_id = infer_plan_id(task_id)
    session_id = create_agent_session(
        plan_id, task_id or "", session_kind, model, reasoning_effort,
        service_tier or "default", mode, workspace,
    )
    proc = subprocess.Popen(
        [exe, "app-server", "--stdio"],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS[task_id] = proc
    stderr_buf = []
    messages = []
    stdout_queue = queue.Queue()
    send_lock = threading.Lock()
    deferred_messages = []

    def drain_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_buf.append(line)
                log(task_id, "stderr", line.rstrip("\n"))
        except Exception:
            pass

    threading.Thread(target=drain_stderr, daemon=True).start()

    def drain_stdout():
        try:
            for line in iter(proc.stdout.readline, ""):
                stdout_queue.put(("line", line))
        finally:
            stdout_queue.put(("eof", ""))

    # Reading App Server stdout on its own thread avoids a subtle interaction
    # between select() and TextIOWrapper's user-space read buffer: one read can
    # contain several JSON messages, while the file descriptor then appears
    # non-readable even though another complete line is already buffered.
    threading.Thread(target=drain_stdout, daemon=True).start()

    def send(obj):
        with send_lock:
            if proc.poll() is not None:
                raise RuntimeError("Codex App Server process is no longer running")
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def read_message(deadline):
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                kind, line = stdout_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if kind == "eof":
                break
            try:
                return json.loads(line)
            except Exception:
                log(task_id, "stdout", line.rstrip("\n"))
        detail = "".join(stderr_buf)[-2000:]
        raise RuntimeError(f"Codex app-server bağlantısı sonlandı veya zaman aşımına uğradı. {detail}".strip())

    def persist(message):
        messages.append(message)
        raw = json.dumps(message, ensure_ascii=False)
        record_codex_event(session_id, task_id or "", plan_id, raw)
        log(task_id, "stdout", pretty_codex_event(raw))

    def answer_server_request(message):
        method = str(message.get("method") or "")
        if not message.get("id") or not method:
            return
        if "requestApproval" in method:
            # AgentDock's controlled worker path never auto-approves an
            # unexpected server approval request. The user can steer/retry.
            send({"id": message["id"], "result": {"decision": "decline"}})
            log(task_id, "supervisor", "App Server approval request declined by AgentDock safety policy")

    def request(request_id, method, params):
        send({"id": request_id, "method": method, "params": params})
        deadline = time.time() + 45
        while time.time() < deadline:
            message = read_message(deadline)
            persist(message)
            if message.get("method"):
                answer_server_request(message)
            if message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
            if message.get("method"):
                # JSON-RPC notifications are allowed to arrive before the
                # response to a request. Keep them for the turn event loop
                # instead of losing a fast completion notification.
                deferred_messages.append(message)
        raise RuntimeError(f"Codex app-server {method} isteği zaman aşımına uğradı")

    def next_event(deadline):
        if deferred_messages:
            return deferred_messages.pop(0)
        return read_message(deadline)

    final = ""
    completed_status = "failed"
    try:
        request(1, "initialize", {
            "clientInfo": {"name": "agentdock", "title": "AgentDock", "version": "0.12.0"},
            "capabilities": {"experimentalApi": True},
        })
        send({"method": "initialized", "params": {}})
        thread_params = {
            "model": model or DEFAULT_WORKER,
            "cwd": workspace,
            "serviceName": "agentdock",
            "serviceTier": service_tier or "default",
        }
        if resume_thread_id:
            thread_result = request(2, "thread/resume", {"threadId": resume_thread_id})
        else:
            thread_result = request(2, "thread/start", thread_params)
        thread = thread_result.get("thread") if isinstance(thread_result, dict) else {}
        thread_id = str((thread or {}).get("id") or resume_thread_id or "")
        if thread_id:
            execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (thread_id, session_id))
            if plan_id and session_kind.startswith("orchestrator"):
                execute(
                    "UPDATE plans SET orchestrator_thread_id=? WHERE id=? AND (orchestrator_thread_id='' OR orchestrator_thread_id=?)",
                    (thread_id, plan_id, thread_id),
                )

        turn_params = {
            "threadId": thread_id,
            "input": app_server_input_items(prompt, images, task_id),
            "cwd": workspace,
            "model": model or DEFAULT_WORKER,
            "effort": reasoning_effort or DEFAULT_WORKER_EFFORT,
            "serviceTier": service_tier or "default",
            "approvalPolicy": "never",
            "sandboxPolicy": _app_server_sandbox(workspace, mode),
            "summary": "concise",
        }
        if output_schema:
            schema_path = Path(output_schema).expanduser().resolve()
            if not schema_path.is_file():
                raise RuntimeError(f"Codex output schema bulunamadı: {schema_path}")
            turn_params["outputSchema"] = json.loads(schema_path.read_text())
        turn_result = request(3, "turn/start", turn_params)
        turn = turn_result.get("turn") if isinstance(turn_result, dict) else {}
        turn_id = str((turn or {}).get("id") or "")
        if turn_id:
            execute("UPDATE agent_sessions SET turn_id=? WHERE id=?", (turn_id, session_id))
        if task_id and thread_id and turn_id:
            with APP_SERVER_CONTROLS_LOCK:
                APP_SERVER_CONTROLS[task_id] = {
                    "send": send,
                    "interrupt": lambda: send({
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    }),
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "next_request_id": 1000,
                    "pending": {},
                }
        deadline = time.time() + 900
        while time.time() < deadline:
            message = next_event(deadline)
            persist(message)
            if message.get("method"):
                answer_server_request(message)
            if message.get("id"):
                with APP_SERVER_CONTROLS_LOCK:
                    control = APP_SERVER_CONTROLS.get(task_id) if task_id else None
                    message_id = control.get("pending", {}).pop(message["id"], None) if control else None
                if message_id:
                    if message.get("error"):
                        error = str(message["error"])
                        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("failed", error, message_id))
                        log(task_id, "manual", f"App Server steer failed: {error}")
                    else:
                        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("delivered", "", message_id))
                        log(task_id, "manual", "App Server steer delivered to the active turn")
            method = message.get("method") or ""
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method in ("turn/completed", "turn/failed"):
                event_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                event_turn_id = str(params.get("turnId") or event_turn.get("id") or "")
                if not turn_id or not event_turn_id or event_turn_id == turn_id:
                    completed_status = str(event_turn.get("status") or params.get("status") or ("failed" if method == "turn/failed" else "completed"))
                    break
        else:
            raise RuntimeError("Codex app-server turn zaman aşımına uğradı")

        if completed_status not in ("completed", "complete", "success"):
            raise RuntimeError(f"Codex turn tamamlanmadı: {completed_status}")
        final = parse_codex_final("\n".join(json.dumps(x, ensure_ascii=False) for x in messages))
        if not final:
            final = "App Server turn completed without an agent message."
        finish_agent_session(session_id, "completed", final)
        return final
    except Exception as exc:
        current_status = (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") if task_id else ""
        stopped = current_status in ("cancelled", "paused_by_user", "pausing") or plan_is_paused(plan_id)
        finish_agent_session(session_id, "cancelled" if stopped else "failed", str(exc))
        raise
    finally:
        with APP_SERVER_CONTROLS_LOCK:
            control = APP_SERVER_CONTROLS.pop(task_id, None) if task_id else None
            pending_message_ids = list((control or {}).get("pending", {}).values())
        for message_id in pending_message_ids:
            execute("UPDATE task_messages SET status=?,error=? WHERE id=? AND status=?", ("failed", "App Server turn ended before this message was delivered", message_id, "sending"))
        with RUNNERS_LOCK:
            if task_id:
                RUNNERS.pop(task_id, None)
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


def run_codex(prompt, workspace, mode="read", model="", task_id=None, reasoning_effort="", service_tier="default",
              images=None, resume_thread_id="", session_kind="worker", output_schema=""):
    if CODEX_TRANSPORT in ("app-server", "app_server"):
        return run_codex_app_server(prompt, workspace, mode, model, task_id, reasoning_effort, service_tier,
                                    images=images, resume_thread_id=resume_thread_id, session_kind=session_kind,
                                    output_schema=output_schema)
    workspace = str(Path(workspace).expanduser().resolve())
    if not Path(workspace).is_dir():
        raise RuntimeError(f"Workspace bulunamadı: {workspace}")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    plan_id = infer_plan_id(task_id)
    session_id = create_agent_session(plan_id, task_id or "", session_kind, model, reasoning_effort, service_tier, mode, workspace)
    if resume_thread_id:
        # The CLI's resume stream may not repeat a thread.started event. Keep
        # the new turn record bound to the known conversation immediately.
        execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (resume_thread_id, session_id))
    args = [
        exe,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write" if mode == "write" else "read-only",
        "--cd",
        workspace,
    ]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    if service_tier:
        args += ["-c", f'service_tier="{service_tier}"']
    if output_schema:
        schema_path = Path(output_schema).expanduser().resolve()
        if not schema_path.is_file():
            raise RuntimeError(f"Codex output schema bulunamadı: {schema_path}")
        args += ["--output-schema", str(schema_path)]
    image_paths = [str(Path(x).expanduser().resolve()) for x in (images or []) if Path(x).expanduser().is_file()]
    if resume_thread_id:
        args += ["resume", resume_thread_id]
        for path in image_paths:
            args += ["--image", path]
        args += [prompt]
    else:
        for path in image_paths:
            args += ["--image", path]
        args += [prompt]
    action = f"resume={resume_thread_id[:12]}" if resume_thread_id else "new-thread"
    log(task_id, "system", f"launch {action} model={model or 'default'} effort={reasoning_effort or 'default'} speed={service_tier or 'default'} mode={mode} cwd={workspace}")
    p = subprocess.Popen(
        args,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS[task_id] = p
    out, err = [], []

    def pump(stream, sink, name):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if name == "stdout":
                record_codex_event(session_id, task_id or "", plan_id, line)
                display = pretty_codex_event(line)
            else:
                display = line.rstrip("\n")
            log(task_id, name, display)
        stream.close()

    t1 = threading.Thread(target=pump, args=(p.stdout, out, "stdout"), daemon=True)
    t2 = threading.Thread(target=pump, args=(p.stderr, err, "stderr"), daemon=True)
    t1.start(); t2.start()
    code = p.wait(); t1.join(); t2.join()
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS.pop(task_id, None)
    stdout, stderr = "".join(out), "".join(err)
    final = parse_codex_final(stdout)
    if code != 0:
        current_status = (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") if task_id else ""
        stopped = current_status in ("cancelled", "paused_by_user", "pausing") or plan_is_paused(plan_id)
        finish_agent_session(session_id, "cancelled" if stopped else "failed", final)
        raise RuntimeError((stderr or stdout or f"process exit {code}")[-12000:])
    finish_agent_session(session_id, "completed", final)
    return final


def steer_app_server(task_id, message_id, prompt, image_paths=None):
    """Deliver a user message to an active App Server turn when possible."""
    with APP_SERVER_CONTROLS_LOCK:
        control = APP_SERVER_CONTROLS.get(task_id)
        if not control:
            return False
        request_id = control["next_request_id"]
        control["next_request_id"] += 1
        control["pending"][request_id] = message_id
        thread_id = control["thread_id"]
        turn_id = control["turn_id"]

    request = {
        "id": request_id,
        "method": "turn/steer",
        "params": {
            "threadId": thread_id,
            "input": app_server_input_items(prompt, image_paths, task_id),
            "expectedTurnId": turn_id,
        },
    }
    try:
        control["send"](request)
        log(task_id, "manual", "user message sent to the active App Server turn")
        return True
    except Exception as exc:
        with APP_SERVER_CONTROLS_LOCK:
            control["pending"].pop(request_id, None)
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("failed", str(exc), message_id))
        log(task_id, "manual", f"App Server steer could not be sent: {exc}")
        return False


def interrupt_app_server(task_id):
    """Ask an active App Server turn to stop without opening a new thread."""
    with APP_SERVER_CONTROLS_LOCK:
        control = APP_SERVER_CONTROLS.get(task_id)
        interrupt = (control or {}).get("interrupt")
    if not interrupt:
        return False
    try:
        interrupt()
        log(task_id, "supervisor", "interrupt requested; preserving this worker conversation for resume")
        return True
    except Exception as exc:
        log(task_id, "stderr", f"App Server interrupt could not be sent: {exc}")
        return False


def orchestrator_models(requested):
    if requested == "auto-best":
        return ["gpt-6-astra", "gpt-5.6-sol"]
    return [requested or DEFAULT_ORCHESTRATOR]


def run_orchestrator(prompt, workspace, requested_model, task_id=None, reasoning_effort="", service_tier="default", mode="read", transient_retries=0, images=None, output_schema="", resume_thread_id="", session_kind="orchestrator"):
    errors = []
    bound_thread_id = resume_thread_id or ""
    for model in orchestrator_models(requested_model):
        attempt = 0
        while True:
            try:
                return run_codex(
                    prompt, workspace, mode, model, task_id, reasoning_effort, service_tier,
                    images=images, resume_thread_id=resume_thread_id, session_kind=session_kind,
                    output_schema=output_schema,
                ), model
            except Exception as e:
                err = str(e)
                if task_id and plan_is_paused(infer_plan_id(task_id)):
                    # A user pause must stop model fallback/retry as well. The
                    # persisted mission state owns the later resume decision.
                    raise
                if attempt < transient_retries and is_transient_error(err):
                    attempt += 1
                    delay = 2 if attempt == 1 else 5
                    log(task_id, "supervisor", f"self-heal: orchestrator transient failure on {model}; retry {attempt}/{transient_retries} in {delay}s")
                    time.sleep(delay)
                    continue
                errors.append(f"{model}: {e}")
                if not bound_thread_id:
                    latest = latest_orchestrator_session(infer_plan_id(task_id)) if task_id else None
                    bound_thread_id = (latest or {}).get("thread_id") or ""
                    resume_thread_id = bound_thread_id
                break
        if requested_model != "auto-best":
            break
    raise RuntimeError("\n\n".join(errors))


def _orchestrator_lock(plan_id):
    with ORCHESTRATOR_TURN_LOCKS_LOCK:
        lock = ORCHESTRATOR_TURN_LOCKS.get(plan_id)
        if lock is None:
            lock = threading.RLock()
            ORCHESTRATOR_TURN_LOCKS[plan_id] = lock
        return lock


def _latest_orchestrator_thread(plan_id):
    session = latest_orchestrator_session(plan_id)
    return (session or {}).get("thread_id") or "", (session or {}).get("turn_id") or ""


def _orchestrator_turn_prompt(purpose, context):
    return f"""AGENTDOCK CONTROL-PLANE TURN

TURN PURPOSE: {purpose}

This is a continuation of the same mission-scoped orchestrator conversation.
Do not create workers or a new task graph unless the turn purpose is initial_disposition.
Respect persisted mission state and the exact output contract supplied by the caller.

{context}
"""


def run_mission_orchestrator_turn(plan_id, purpose, context, expected_output_schema="", mode="read",
                                  images=None, transient_retries=0, requested_model=None):
    """The only production entry point for a mission's orchestrator turns.

    A per-mission lock serializes turns while the database preserves the queue
    and thread identity across process restarts.
    """
    if purpose not in ORCHESTRATOR_PURPOSES:
        raise ValueError(f"Unknown orchestrator turn purpose: {purpose}")
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    lock = _orchestrator_lock(plan_id)
    turn_record_id = str(uuid.uuid4())
    context_payload = {
        "purpose": purpose,
        "context": str(context or "")[-120000:],
        "expected_output_schema": str(expected_output_schema or ""),
    }
    with lock:
        plan = one("SELECT * FROM plans WHERE id=?", (plan_id,)) or plan
        existing_thread = "" if purpose == "reconstruct" else str(plan.get("orchestrator_thread_id") or "").strip()
        if not existing_thread and purpose != "reconstruct":
            adopted_thread, _ = _latest_orchestrator_thread(plan_id)
            existing_thread = adopted_thread
        if not existing_thread and purpose not in {"initial_disposition", "reconstruct"}:
            message = "Mission orchestrator thread is unavailable; explicit context reconstruction is required."
            execute("UPDATE plans SET orchestrator_turn_status=?,orchestrator_last_error=? WHERE id=?",
                    ("attention", message, plan_id))
            raise RuntimeError(message)
        execute(
            """INSERT INTO orchestrator_turns(
                id,plan_id,thread_id,purpose,status,context_json,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                turn_record_id, plan_id, existing_thread, purpose, "queued",
                json.dumps(context_payload, ensure_ascii=False), now(),
            ),
        )
        execute(
            "UPDATE plans SET orchestrator_turn_status=?,orchestrator_last_error=? WHERE id=?",
            ("running", "", plan_id),
        )
        log(
            orchestrator_log_id(plan_id),
            "supervisor",
            f"orchestrator turn queued · purpose={purpose} · "
            f"{'resume=' + existing_thread[:12] if existing_thread else 'new-thread'}",
        )
        execute(
            "UPDATE orchestrator_turns SET status=?,started_at=? WHERE id=?",
            ("running", now(), turn_record_id),
        )
        try:
            prompt = _orchestrator_turn_prompt(purpose, context)
            model = requested_model or plan.get("orchestrator_model") or DEFAULT_ORCHESTRATOR
            recovery = recovery_settings(plan)
            text, used_model = run_orchestrator(
                prompt,
                plan["workspace"],
                model,
                orchestrator_log_id(plan_id),
                plan.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT,
                plan.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER,
                mode=mode,
                transient_retries=transient_retries if transient_retries is not None else (1 if recovery.get("auto_retry_transient") else 0),
                images=images or [],
                output_schema=expected_output_schema,
                resume_thread_id=existing_thread,
                session_kind="orchestrator",
            )
            actual_thread, turn_id = _latest_orchestrator_thread(plan_id)
            actual_thread = actual_thread or existing_thread
            if not actual_thread:
                raise RuntimeError("Orchestrator response completed without a resumable thread id.")
            if existing_thread and actual_thread != existing_thread and purpose != "reconstruct":
                raise RuntimeError(
                    "Orchestrator resume returned a different thread; explicit context reconstruction is required."
                )
            session = latest_orchestrator_session(plan_id) or {}
            usage = orchestrator_session_usage(session.get("id"))
            response_json = "{}"
            try:
                parsed_response = extract_json(text)
                if isinstance(parsed_response, dict):
                    response_json = json.dumps(parsed_response, ensure_ascii=False)
            except Exception:
                pass
            generation = int(plan.get("orchestrator_generation") or 1)
            if purpose == "reconstruct":
                generation = max(1, generation + 1)
            elif not existing_thread:
                generation = max(1, generation)
            execute(
                """UPDATE plans SET orchestrator_thread_id=?,orchestrator_generation=?,
                   orchestrator_turn_status=?,orchestrator_last_turn_id=?,
                   orchestrator_last_error=?,orchestrator_used=? WHERE id=?""",
                (actual_thread, generation, "completed", turn_id, "", used_model, plan_id),
            )
            execute(
                """UPDATE orchestrator_turns SET thread_id=?,turn_id=?,status=?,
                   response_text=?,response_json=?,usage_json=?,finished_at=? WHERE id=?""",
                (actual_thread, turn_id, "completed", str(text or "")[-120000:], response_json,
                 json.dumps(usage, ensure_ascii=False), now(), turn_record_id),
            )
            log(
                orchestrator_log_id(plan_id),
                "supervisor",
                f"orchestrator turn completed · purpose={purpose} · thread={actual_thread[:12]}",
            )
            record_control_event(plan_id, "agentdock.orchestrator_turn", {
                "purpose": purpose,
                "status": "completed",
                "thread_id": actual_thread,
                "turn_id": turn_id,
            })
            return {
                "text": text,
                "model": used_model,
                "thread_id": actual_thread,
                "turn_id": turn_id,
                "turn_record_id": turn_record_id,
                "usage": usage,
            }
        except Exception as exc:
            message = str(exc)
            actual_thread, turn_id = _latest_orchestrator_thread(plan_id)
            session = latest_orchestrator_session(plan_id) or {}
            usage = orchestrator_session_usage(session.get("id"))
            # A failed resume is attention, never a silent new conversation.
            turn_status = "paused" if plan_is_paused(plan_id) else "attention"
            execute(
                """UPDATE plans SET orchestrator_thread_id=COALESCE(NULLIF(orchestrator_thread_id,''),?),
                   orchestrator_turn_status=?,orchestrator_last_turn_id=?,
                   orchestrator_last_error=? WHERE id=?""",
                (actual_thread, turn_status, turn_id, "" if turn_status == "paused" else message, plan_id),
            )
            execute(
                """UPDATE orchestrator_turns SET thread_id=?,turn_id=?,status=?,usage_json=?,error=?,finished_at=? WHERE id=?""",
                (actual_thread or existing_thread, turn_id, "failed", json.dumps(usage, ensure_ascii=False), message[-12000:], now(), turn_record_id),
            )
            log(orchestrator_log_id(plan_id), "supervisor", f"orchestrator turn failed · purpose={purpose}: {message}")
            record_control_event(plan_id, "agentdock.orchestrator_turn", {
                "purpose": purpose,
                "status": "failed",
                "thread_id": actual_thread or existing_thread,
                "turn_id": turn_id,
                "error": message,
            })
            raise


def planner_prompt(goal, agents, worker_model, worker_effort, worker_tier, max_parallel, snapshot=None, planner_instruction=""):
    roster = "\n".join(
        [f"- {a['id']}: {a['name']} — {a['role']} — default mode={a['mode']}" for a in agents]
    )
    snapshot = snapshot or {}
    extra = f"\nUSER / CONTROL-PLANE INSTRUCTION:\n{planner_instruction}\n" if planner_instruction else ""
    return f"""You are the root orchestrator of a local multi-agent coding harness. Workers use a smaller execution model and MUST NOT be forced to make product, architecture, scope, or prioritization decisions.

GOAL:
{goal}

MISSION DISPOSITION FIRST:
Before creating any worker task, inspect the workspace and decide what this mission actually requires. Creating tasks is optional. A mission that is already satisfied, asks only for an explanation, needs a missing user/product decision, or cannot be performed safely must have zero tasks.

DETERMINISTIC WORKSPACE SNAPSHOT (read-only, captured before this model call):
{json.dumps(snapshot, ensure_ascii=False, indent=2)}
{extra}

WORKER DEFAULT:
model={worker_model}
reasoning_effort={worker_effort}
speed={worker_tier}

MAX PARALLEL WORKERS:
{max_parallel}

AVAILABLE WORKER PROFILES:
{roster}

Return ONLY valid JSON with this exact shape:
{{
  "title": "short meaningful mission title, without implementation detail",
  "decision": "already_satisfied|answer_only|needs_user_input|blocked|execute",
  "reason": "short reason for the disposition",
  "evidence": ["workspace facts or checks actually inspected"],
  "final_response": "user-facing result when no worker task is needed, or an empty string for execute",
  "questions": ["concrete question for the user when decision is needs_user_input"],
  "tasks": [
    {{
      "title": "...",
      "agent_id": "one id above",
      "mode": "read|write",
      "depends_on": [0],
      "contract": {{
        "objective": "one unambiguous outcome",
        "context": "facts the worker needs to understand why this task exists",
        "scope": {{"in_scope": ["..."], "out_of_scope": ["..."]}},
        "allowed_paths": ["exact files, path prefixes or bounded globs"],
        "required_inputs": ["dependency results, symbols, invariants, interfaces"],
        "implementation_steps": ["ordered concrete steps"],
        "acceptance_criteria": ["binary or directly verifiable done conditions"],
        "verification_commands": ["commands to run, or explicit inspection checks for read tasks"],
        "expected_output": ["what the worker must report/produce"],
        "escalation_conditions": ["ambiguities that must return BLOCKED_NEEDS_ORCHESTRATOR instead of being decided locally"],
        "decision_policy": "explicitly state what local implementation choices are allowed and what decisions are reserved for the orchestrator"
      }}
    }}
  ]
}}

Rules:
- `title` should be a concise human-readable label for the mission, not a copy of the full prompt.
- `tasks` may contain 0-12 tasks.
- Use `already_satisfied` only when evidence proves the requested outcome already holds.
- Use `answer_only` for a direct explanation/report that does not require a worker.
- Use `needs_user_input` when a product, scope, priority, credential, or other user decision is missing. Include 1-3 concrete questions and keep tasks empty.
- Use `blocked` for a safety, authority, permission, or destructive-operation boundary. Never present a blocked mission as complete; keep tasks empty.
- Use `execute` only when work is actually required, and then include at least one task. A simple change or verification may use exactly one task.
- If the user explicitly asks to create an execution plan anyway, honor that request with the smallest honest read or write task graph; do not invent changes.
- Decompose aggressively when independent work can run in parallel.
- Dependencies are zero-based indexes of earlier tasks.
- Tasks with no dependency relationship may run at the same time.
- Use read for architecture, investigation and review. Use write only when files must change.
- Parallel write tasks MUST be logically independent and target disjoint files/modules whenever possible.
- Do not serialize tasks merely because they belong to the same goal.
- Include integration-aware verification after implementation when appropriate.
- Include a final independent review task for code-changing goals when it materially improves safety; do not manufacture a second task for a simple change.
- Every contract must be sufficiently detailed for a smaller worker model to execute without re-planning the task.
- Give bounded allowed_paths. If an exact file is not yet known, specify a narrow directory/pattern and the exact symbol/behavior to locate.
- Acceptance criteria must be testable. Avoid vague requirements such as "make it robust" without defining what robust means here.
- Workers may make low-level implementation choices only when they preserve the stated interfaces and acceptance criteria.
- Architecture, scope expansion, product tradeoffs, dependency changes, destructive operations, and ambiguous behavior are reserved for you, the orchestrator.
- Never ask workers to commit, merge, cherry-pick, create worktrees, or modify git history; the harness owns Git isolation.
"""


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Orchestrator geçerli JSON döndürmedi")
    return json.loads(m.group(0))


def deterministic_mission_title(goal):
    """Create a short stable UI title when the planner did not provide one."""
    text = re.sub(r"\s+", " ", str(goal or "").replace("\u200b", " ")).strip()
    if not text:
        return "New mission"
    # Keep the first complete thought where possible; this is intentionally
    # deterministic so a transient planner response cannot rename a mission.
    sentence = re.split(r"(?<=[.!?])\s+|\n+", text, maxsplit=1)[0].strip(" .!?-:")
    sentence = re.sub(r"^(?:öncelikle|lütfen|please|şunu|bunu)\s+", "", sentence, flags=re.I).strip()
    if not sentence:
        sentence = text
    if len(sentence) > 78:
        sentence = sentence[:78].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return sentence or "New mission"


def normalize_planner_result(obj):
    """Validate disposition invariants before materializing any tasks."""
    if not isinstance(obj, dict):
        raise ValueError("Orchestrator plan response must be an object")
    decision = str(obj.get("decision") or "").strip()
    tasks = obj.get("tasks") if isinstance(obj.get("tasks"), list) else []
    # Keep compatibility with pre-disposition planner responses already in the
    # local database/test fixtures, while all new model calls use the strict
    # schema above.
    if not decision and tasks:
        decision = "execute"
    if decision not in MISSION_DECISIONS:
        raise ValueError(f"Invalid mission disposition: {decision or 'missing'}")
    if len(tasks) > 12:
        raise ValueError("Orchestrator returned more than 12 tasks")
    if decision == "execute" and not tasks:
        raise ValueError("execute disposition requires at least one task")
    if decision != "execute" and tasks:
        raise ValueError(f"{decision} disposition must not contain tasks")

    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), list) else []
    questions = obj.get("questions") if isinstance(obj.get("questions"), list) else []
    evidence = [str(x) for x in evidence if str(x).strip()][:32]
    questions = [str(x) for x in questions if str(x).strip()][:12]
    reason = str(obj.get("reason") or "").strip()
    final_response = str(obj.get("final_response") or "").strip()
    if not reason and decision == "execute" and tasks:
        reason = "Execution tasks returned by a legacy planner response."
    if not reason:
        raise ValueError("Planner disposition reason is required")
    if decision == "already_satisfied" and not evidence:
        raise ValueError("already_satisfied requires evidence")
    if decision == "needs_user_input" and not questions:
        raise ValueError("needs_user_input requires at least one question")
    if decision == "blocked" and not final_response:
        final_response = reason
    if decision in ("already_satisfied", "answer_only") and not final_response:
        final_response = reason
    title = str(obj.get("title") or "").strip()
    if len(title) > 120:
        title = title[:120].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return {
        "title": title,
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "final_response": final_response,
        "questions": questions,
        "tasks": tasks,
    }


def extract_worker_consultation(output):
    """Read the structured worker escalation, with a legacy-text fallback."""
    text = str(output or "").strip()
    candidate = None
    marker = "BLOCKED_NEEDS_ORCHESTRATOR:"
    if marker in text:
        tail = text.split(marker, 1)[1].strip()
        try:
            candidate = extract_json(tail)
        except Exception:
            candidate = None
        if not isinstance(candidate, dict):
            candidate = {
                "type": "needs_orchestrator",
                "question": tail[:4000] or "Worker requires an orchestrator decision.",
                "reason": "Worker reached a reserved decision boundary.",
                "evidence": [tail[:4000]] if tail else [],
                "options": [],
            }
    else:
        try:
            obj = extract_json(text)
            if isinstance(obj, dict) and obj.get("type") == "needs_orchestrator":
                candidate = obj
        except Exception:
            candidate = None
    if not isinstance(candidate, dict) or candidate.get("type") != "needs_orchestrator":
        return None
    question = str(candidate.get("question") or "").strip()
    reason = str(candidate.get("reason") or "").strip()
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    options = candidate.get("options") if isinstance(candidate.get("options"), list) else []
    if not question:
        question = "Worker requires an orchestrator decision before continuing."
    if not reason:
        reason = "Worker reported a material ambiguity and stopped before guessing."
    return {
        "type": "needs_orchestrator",
        "question": question[:6000],
        "reason": reason[:6000],
        "evidence": [str(x) for x in evidence if str(x).strip()][:32],
        "options": [str(x) for x in options if str(x).strip()][:12],
    }


def normalize_consultation_result(obj):
    """Validate the bounded response the root orchestrator gives a worker."""
    if not isinstance(obj, dict):
        raise ValueError("Orchestrator consultation response must be an object")
    action = str(obj.get("action") or "").strip()
    # Accept the old escalation vocabulary only while migrating old missions.
    if action == "retry":
        action = "revise_contract"
    elif action == "stop":
        action = "block_mission"
    if action not in ORCHESTRATOR_ACTIONS:
        raise ValueError(f"Invalid orchestrator consultation action: {action or 'missing'}")
    reason = str(obj.get("reason") or "").strip()
    if not reason:
        raise ValueError("Orchestrator consultation reason is required")
    worker_message = str(obj.get("worker_message") or obj.get("note") or "").strip()
    revised = obj.get("revised_contract")
    if not isinstance(revised, dict):
        revised = obj.get("contract") if isinstance(obj.get("contract"), dict) else {}
    questions = obj.get("questions") if isinstance(obj.get("questions"), list) else []
    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), list) else []
    questions = [str(x) for x in questions if str(x).strip()][:12]
    evidence = [str(x) for x in evidence if str(x).strip()][:32]
    if action in ("answer_worker", "revise_contract") and not worker_message:
        worker_message = reason
    if action == "revise_contract" and not revised:
        raise ValueError("revise_contract requires a complete revised_contract")
    if action == "ask_user" and not questions:
        raise ValueError("ask_user requires at least one question")
    return {
        "action": action,
        "reason": reason,
        "worker_message": worker_message,
        "revised_contract": revised,
        "questions": questions,
        "evidence": evidence,
    }


def task_dependency_context(task):
    deps = json.loads(task.get("depends_json") or "[]")
    if not deps:
        return ""
    dep_rows = []
    for d in deps:
        r = one("SELECT title,output,error,status FROM tasks WHERE plan_id=? AND seq=?", (task["plan_id"], d))
        if r:
            dep_rows.append(
                f"DEPENDENCY {d+1}: {r['title']}\nSTATUS: {r['status']}\nRESULT:\n{(r['output'] or r['error'])[-6000:]}"
            )
    return "\n\n".join(dep_rows)


def make_task_prompt(plan, task, agent):
    dep_context = task_dependency_context(task)
    contract = safe_json(task.get("contract_json"), {})
    resume_message = str(task.get("worker_resume_message") or "").strip()
    resume_block = f"""

ROOT ORCHESTRATOR DECISION — CONTINUE THIS SAME TASK:
{resume_message}

This is an authoritative control-plane instruction for the current task. Keep
the existing worker conversation and continue from its previous context; do
not create a new task or reinterpret the parent goal.
""" if resume_message else ""
    return f"""You are an execution worker in AgentDock. The root orchestrator has already made the task-level decisions. Do NOT re-plan the parent goal.

WORKER PROFILE: {agent['name']}
ROLE / BRIEF:
{agent['role']}

PARENT GOAL:
{plan['goal']}

TASK:
{task['title']}

EXECUTION CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}
{resume_block}

DEPENDENCY RESULTS:
{dep_context or 'None.'}

NON-NEGOTIABLE WORK RULES:
- Execute only this contract. Do not expand scope or redesign adjacent systems.
- Stay inside the provided workspace and the contract's allowed_paths.
- You are one parallel worker. Do not coordinate via Git branches/commits; the harness handles isolation and integration.
- Do not make architecture, product, scope, prioritization, dependency, or destructive-operation decisions.
- Low-level implementation choices are allowed only when the contract's decision_policy permits them and all stated interfaces/invariants remain unchanged.
- If a reserved decision or material ambiguity is required, STOP before guessing and return a single JSON object in this exact shape:
  {{"type":"needs_orchestrator","question":"...","reason":"...","evidence":["..."],"options":["..."]}}
  Do not invent product, architecture, legal, identity, contact or destructive-operation facts.
- Do not claim a verification passed unless you actually ran or inspected it.
- If the task is read-only, do not modify files.
- Finish with a concise result covering deliverables, files changed/findings, verification performed, and remaining risks.
"""


def task_model(plan, agent, task=None):
    task = task or {}
    return (task.get("model_override") or "").strip() or (agent.get("model") or agent.get("agent_model") or "").strip() or plan.get("worker_model") or DEFAULT_WORKER


def task_effort(plan, agent, task=None):
    task = task or {}
    return (task.get("reasoning_effort_override") or "").strip() or (agent.get("reasoning_effort") or agent.get("agent_effort") or "").strip() or plan.get("worker_effort") or DEFAULT_WORKER_EFFORT


def task_tier(plan, agent, task=None):
    task = task or {}
    return (task.get("service_tier_override") or "").strip() or (agent.get("service_tier") or agent.get("agent_tier") or "").strip() or plan.get("worker_tier") or DEFAULT_WORKER_TIER


def sanitize_branch_component(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:40] or "task"


def plan_paths(plan_id):
    base = WORKTREE_ROOT / plan_id
    return base, base / "integration"


def remove_worktree(repo_root, path):
    path = Path(path)
    if path.exists():
        git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def delete_branch(repo_root, branch):
    git(repo_root, "branch", "-D", branch, check=False)


def prepare_integration(plan):
    info = repo_info(plan["workspace"])
    if not info["is_git"]:
        raise RuntimeError("Paralel write agent'ları için workspace bir Git repository içinde olmalı.")
    if info["dirty"]:
        # The explicit execution preflight choice protects these changes in the
        # user's checkout. Worker isolation starts from HEAD and never writes
        # into this dirty checkout; the final apply gate still rechecks it.
        log(orchestrator_log_id(plan["id"]), "supervisor", "working tree is dirty but explicitly accepted; isolated workers start from HEAD")
    repo_root = Path(info["root"])
    base_dir, integration_dir = plan_paths(plan["id"])
    base_dir.mkdir(parents=True, exist_ok=True)
    branch = f"agentdock/{plan['id']}/integration"
    remove_worktree(repo_root, integration_dir)
    delete_branch(repo_root, branch)
    git(repo_root, "worktree", "add", "-b", branch, str(integration_dir), info["head"])
    work_rel = Path(info["rel"])
    integration_workspace = (integration_dir / work_rel).resolve()
    execute(
        "UPDATE plans SET base_commit=?, integration_workspace=?, error=? WHERE id=?",
        (info["head"], str(integration_workspace), "", plan["id"]),
    )
    return {
        "repo_root": repo_root,
        "base_commit": info["head"],
        "workspace_rel": work_rel,
        "integration_dir": integration_dir,
        "integration_workspace": integration_workspace,
        "integration_branch": branch,
    }


def create_worker_worktree(ctx, plan, task, base_commit):
    base_dir, _ = plan_paths(plan["id"])
    existing_workspace = Path(str(task.get("workspace") or "")).expanduser()
    existing_branch = str(task.get("branch") or "").strip()
    if existing_workspace.is_dir() and existing_branch:
        # A paused worker owns this worktree. Reusing it preserves uncommitted
        # progress and, more importantly, lets the same Codex thread continue
        # without silently rebuilding the task from HEAD.
        expected_root = (base_dir / f"task-{task['seq']+1}-{task['id']}").resolve()
        detected = git_read(existing_workspace, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        worktree_root = Path(detected).expanduser().resolve() if detected else existing_workspace.resolve()
        try:
            existing_workspace.resolve().relative_to(worktree_root)
        except ValueError:
            worktree_root = existing_workspace.resolve()
        # Only reuse a worktree that belongs to this mission/task. If an old
        # path is stale or points elsewhere, the normal isolated worktree
        # creation below is safer than deleting or mutating an unknown folder.
        if worktree_root == expected_root or (
            base_dir.resolve() in worktree_root.parents
            and worktree_root.name == expected_root.name
        ):
            return worktree_root, existing_workspace.resolve(), existing_branch
    wt = base_dir / f"task-{task['seq']+1}-{task['id']}"
    branch = f"agentdock/{plan['id']}/task-{task['seq']+1}-{sanitize_branch_component(task['title'])}-{task['id']}"
    remove_worktree(ctx["repo_root"], wt)
    delete_branch(ctx["repo_root"], branch)
    git(ctx["repo_root"], "worktree", "add", "-b", branch, str(wt), base_commit)
    task_workspace = (wt / ctx["workspace_rel"]).resolve()
    execute("UPDATE tasks SET workspace=?, branch=? WHERE id=?", (str(task_workspace), branch, task["id"]))
    return wt, task_workspace, branch


def commit_worker_changes(wt, task):
    git(wt, "add", "-A")
    changed = bool(git(wt, "status", "--porcelain").stdout.strip())
    if not changed:
        return ""
    msg = f"AgentDock task {task['seq']+1}: {task['title']}"
    git(
        wt,
        "-c", "user.name=AgentDock",
        "-c", "user.email=agentdock@local",
        "commit", "-m", msg,
    )
    return git(wt, "rev-parse", "HEAD").stdout.strip()


def changed_git_paths(repo_root):
    """Return tracked, staged and untracked paths visible in a worker worktree."""
    found = set()
    for args in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = git(repo_root, *args, check=False)
        found.update(x for x in result.stdout.split("\0") if x)
    return sorted(found)


def normalize_allowed_pattern(pattern):
    value = str(pattern or "").strip().replace("\\", "/")
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    value = re.sub(r"\s+only when\b.*$", "", value, flags=re.I)
    value = value.lstrip("./")
    if value.startswith("workspace/"):
        value = value[len("workspace/"):]
    return value.rstrip("/") or "**"


def path_matches_allowed(rel_path, pattern):
    rel = str(rel_path).replace("\\", "/").lstrip("./")
    pat = normalize_allowed_pattern(pattern)
    if pat in ("*", "**"):
        return True
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        return rel == prefix or rel.startswith(prefix + "/")
    return rel == pat or fnmatch.fnmatchcase(rel, pat)


def validate_worker_changes(worktree, task):
    contract = safe_json(task.get("contract_json"), {})
    allowed = contract.get("allowed_paths") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    changed = changed_git_paths(worktree)
    violations = [path for path in changed if not any(path_matches_allowed(path, p) for p in allowed)]
    if violations:
        raise RuntimeError(
            "Worker contract dışındaki dosyaları değiştirdi: " + ", ".join(violations[:20])
        )
    return changed


def validate_read_workspace(workspace, expected_head=None, baseline=None):
    """Validate that a read task preserved the exact pre-task workspace state."""
    if baseline is None:
        baseline = workspace_fingerprint(workspace)
        if expected_head is not None and baseline.get("head") != expected_head:
            raise RuntimeError("Read-only task workspace HEAD'i task başlamadan önce beklenen commit ile eşleşmiyor.")
    after = workspace_fingerprint(workspace)
    changes = fingerprint_diff(baseline, after)
    if changes:
        raise RuntimeError(
            "Read-only task workspace'i değiştirdi; değişen alan/dosyalar: " + ", ".join(changes[:20])
        )
    return after


def create_worker_consultation(plan, task, output):
    request = extract_worker_consultation(output)
    if not request:
        return None
    latest = latest_agent_session(task["id"]) or {}
    worker_thread_id = latest.get("thread_id") or task.get("worker_thread_id") or ""
    consultation_id = str(uuid.uuid4())
    execute(
        """INSERT INTO consultations(
            id,plan_id,task_id,status,question,reason,evidence_json,options_json,
            worker_thread_id,orchestrator_thread_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            consultation_id,
            plan["id"],
            task["id"],
            "queued",
            request["question"],
            request["reason"],
            json.dumps(request["evidence"], ensure_ascii=False),
            json.dumps(request["options"], ensure_ascii=False),
            worker_thread_id,
            plan.get("orchestrator_thread_id") or "",
            now(),
        ),
    )
    execute(
        """UPDATE tasks SET status=?,output=?,error=?,finished_at=NULL,
           worker_thread_id=?,waiting_reason=?,consultation_id=? WHERE id=?""",
        (
            "waiting_for_orchestrator",
            str(output or "")[-50000:],
            "",
            worker_thread_id,
            request["question"],
            consultation_id,
            task["id"],
        ),
    )
    log(
        task["id"],
        "supervisor",
        f"worker consultation queued · {request['question'][:300]}",
    )
    log(
        orchestrator_log_id(plan["id"]),
        "supervisor",
        f"TASK-{task['seq']+1:03d} waiting for orchestrator · consultation={consultation_id[:12]}",
    )
    record_control_event(plan["id"], "agentdock.consultation", {
        "consultation_id": consultation_id,
        "task_id": task["id"],
        "question": request["question"],
        "reason": request["reason"],
        "evidence": request["evidence"],
        "options": request["options"],
    }, task_id=task["id"])
    write_mission_docs(plan["id"])
    return {**request, "id": consultation_id, "worker_thread_id": worker_thread_id}


def run_task_once(plan, task, workspace, force_mode=None):
    persisted = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    if persisted:
        task = persisted
    if task.get("status") in ("paused_by_user", "pausing"):
        return {"ok": False, "paused": True, "error": "User paused this worker"}
    if task.get("status") == "cancelled":
        return {"ok": False, "cancelled": True, "error": "User cancelled this worker"}
    agent = one("SELECT * FROM agents WHERE id=?", (task["agent_id"],)) or one("SELECT * FROM agents ORDER BY created_at LIMIT 1")
    mode = force_mode or task["mode"]
    model = task_model(plan, agent, task)
    effort = task_effort(plan, agent, task)
    tier = task_tier(plan, agent, task)
    existing_worker_thread = task.get("worker_thread_id") or (latest_agent_session(task["id"]) or {}).get("thread_id") or ""
    execute(
        "UPDATE tasks SET status=?, started_at=?, error=?, workspace=?,waiting_reason=? WHERE id=?",
        ("running", now(), "", str(workspace), "", task["id"]),
    )
    write_mission_docs(plan["id"])
    try:
        output = run_codex(
            make_task_prompt(plan, task, agent),
            workspace,
            mode,
            model,
            task["id"],
            effort,
            tier,
            images=task_input_attachment_paths(plan, task),
            resume_thread_id=existing_worker_thread,
            session_kind="worker",
        )
        current_status = (one("SELECT status FROM tasks WHERE id=?", (task["id"],)) or {}).get("status")
        if current_status in ("paused_by_user", "pausing"):
            write_mission_docs(plan["id"])
            return {"ok": False, "paused": True, "error": "User paused this worker"}
        if task.get("worker_resume_message"):
            execute("UPDATE tasks SET worker_resume_message=? WHERE id=?", ("", task["id"]))
        latest = latest_agent_session(task["id"]) or {}
        worker_thread_id = latest.get("thread_id") or existing_worker_thread
        if worker_thread_id:
            execute("UPDATE tasks SET worker_thread_id=? WHERE id=?", (worker_thread_id, task["id"]))
        consultation = create_worker_consultation(plan, task, output)
        if consultation:
            return {
                "ok": False,
                "waiting_for_orchestrator": True,
                "consultation": consultation,
                "output": output,
                "error": "Worker needs an orchestrator decision",
            }
        # Queued user messages are conversational follow-ups, not a new
        # execution checkpoint. Their replies are persisted in the manual
        # session/timeline by consume_queued_messages; never replace the
        # worker's canonical execution output or turn it into a consultation.
        consume_queued_messages(plan, task, workspace, model, effort, tier)
        execute("UPDATE tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?", ("executed", output, "", now(), task["id"]))
        write_mission_docs(plan["id"])
        return {"ok": True, "output": output}
    except Exception as e:
        current=one("SELECT status FROM tasks WHERE id=?",(task["id"],)) or {}
        latest = latest_agent_session(task["id"]) or {}
        worker_thread_id = latest.get("thread_id") or task.get("worker_thread_id") or ""
        if worker_thread_id:
            execute("UPDATE tasks SET worker_thread_id=? WHERE id=?", (worker_thread_id, task["id"]))
        if current.get("status") in ("paused_by_user", "pausing"):
            write_mission_docs(plan["id"])
            return {"ok": False, "paused": True, "error": "User paused this worker"}
        if current.get("status")=="cancelled":
            write_mission_docs(plan["id"])
            return {"ok": False, "cancelled": True, "error": "User cancelled"}
        execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), now(), task["id"]))
        write_mission_docs(plan["id"])
        return {"ok": False, "error": str(e)}



def run_task_with_recovery(plan, task, workspace, force_mode=None):
    settings = recovery_settings(plan)
    max_retries = 2 if settings.get("auto_retry_transient") else 0
    attempt = 0
    while True:
        result = run_task_once(plan, task, workspace, force_mode=force_mode)
        if result.get("ok") or result.get("blocked") or result.get("paused") or result.get("cancelled") or result.get("waiting_for_orchestrator") or result.get("waiting_for_user"):
            return result
        err = result.get("error") or ""
        kind = "transient" if is_transient_error(err) else "deterministic"
        execute("UPDATE tasks SET last_failure_kind=? WHERE id=?", (kind, task["id"]))
        if kind != "transient" or attempt >= max_retries:
            return result
        attempt += 1
        execute("UPDATE tasks SET retry_count=? WHERE id=?", (attempt, task["id"]))
        execute("UPDATE plans SET recovery_count=recovery_count+1 WHERE id=?", (plan["id"],))
        delay = 2 if attempt == 1 else 5
        log(task["id"], "supervisor", f"self-heal: transient failure detected; retry {attempt}/{max_retries} in {delay}s")
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} transient failure; automatic retry {attempt}/{max_retries}")
        time.sleep(delay)

def run_parallel_task(plan, task, ctx, wave_base_commit):
    persisted = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    if persisted:
        task = persisted
    if task.get("status") in ("paused_by_user", "pausing"):
        return {"task": task, "ok": False, "paused": True, "write": task.get("mode") == "write", "phase": "worker", "error": "User paused this worker"}
    if task.get("status") == "cancelled":
        return {"task": task, "ok": False, "cancelled": True, "write": task.get("mode") == "write", "phase": "worker", "error": "User cancelled this worker"}
    if task["mode"] == "write":
        try:
            wt, workspace, branch = create_worker_worktree(ctx, plan, task, wave_base_commit)
        except Exception as e:
            execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), now(), task["id"]))
            return {"task": task, "ok": False, "write": True, "phase": "worktree", "error": str(e)}
        result = run_task_with_recovery(plan, task, workspace)
        if result.get("paused"):
            log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} paused by user; worktree and worker thread preserved")
            return {"task": task, "ok": False, "paused": True, "write": True, "phase": "worker", "wt": str(wt), "branch": branch, **result}
        # A previous interrupted/failed integration may already have produced
        # a durable worker commit. Keep it as the checkpoint if the resumed
        # turn has no additional file changes.
        commit_hash = str(task.get("commit_hash") or "")
        if result["ok"]:
            if (one("SELECT status FROM tasks WHERE id=?", (task["id"],)) or {}).get("status") in ("paused_by_user", "pausing"):
                return {"task": task, "ok": False, "paused": True, "write": True, "phase": "worker", "wt": str(wt), "branch": branch, **result}
            try:
                changed = validate_worker_changes(wt, task)
                log(task["id"], "supervisor", f"contract path check passed · files={len(changed)}")
                commit_hash = commit_worker_changes(wt, task) or commit_hash
                execute("UPDATE tasks SET commit_hash=? WHERE id=?", (commit_hash, task["id"]))
            except Exception as e:
                result = {"ok": False, "phase": "contract", "error": f"Worker değişiklikleri contract kontrolünden geçemedi: {e}"}
                execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", result["error"], now(), task["id"]))
        return {"task": task, "ok": result["ok"], "write": True, "phase": "worker", "wt": str(wt), "branch": branch, "commit": commit_hash, **result}
    else:
        baseline = workspace_fingerprint(ctx["integration_workspace"])
        execute(
            "UPDATE tasks SET baseline_fingerprint_json=? WHERE id=?",
            (json.dumps(baseline, ensure_ascii=False), task["id"]),
        )
        result = run_task_with_recovery(plan, task, ctx["integration_workspace"], force_mode="read")
        if result.get("paused"):
            return {"task": task, "ok": False, "paused": True, "write": False, "phase": "worker", **result}
        if result.get("ok"):
            try:
                after = validate_read_workspace(ctx["integration_workspace"], baseline=baseline)
                result["fingerprint"] = after
            except Exception as e:
                result = {"ok": False, "phase": "contract", "error": str(e)}
                execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), now(), task["id"]))
        return {"task": task, "ok": result.get("ok", False), "write": False, "phase": "worker", **result}



def merge_conflict_prompt(plan, task, unresolved, cherry_error):
    contract = safe_json(task.get("contract_json"), {})
    return f"""You are the root orchestrator resolving a Git integration conflict between parallel worker results.

MISSION:
{plan['goal']}

CURRENT TASK:
TASK-{task['seq']+1:03d} — {task['title']}

TASK CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

CONFLICTED FILES:
{chr(10).join('- ' + x for x in unresolved) or '- unknown'}

CHERRY-PICK ERROR:
{cherry_error[-4000:]}

The integration worktree is currently in an active cherry-pick conflict state.
Resolve ONLY the conflict markers needed to preserve both already-integrated behavior and this task's explicit acceptance criteria.
You may inspect files and run focused verification. Do not broaden scope, refactor unrelated code, delete user work, change dependencies, or run git commit/cherry-pick/abort/reset commands. The harness owns Git state.
If the conflict cannot be resolved without a product/architecture decision outside the existing contracts, respond exactly with:
BLOCKED_NEEDS_USER: <reason>
Otherwise resolve the files in-place and briefly report what you reconciled.
"""


def resolve_merge_conflict(plan, ctx, result, cherry_error):
    task = result["task"]
    settings = recovery_settings(plan)
    unresolved = [x for x in git(ctx["integration_dir"], "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if x.strip()]
    if settings.get("merge_conflicts") != "orchestrator":
        return False, f"Merge conflict requires attention: {', '.join(unresolved) or cherry_error}"
    log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} integration conflict; root orchestrator is attempting a bounded resolution")
    execute("UPDATE plans SET recovery_count=recovery_count+1 WHERE id=?", (plan["id"],))
    try:
        retries = 1 if settings.get("auto_retry_transient") else 0
        turn = run_mission_orchestrator_turn(
            plan["id"],
            "failure_recovery",
            merge_conflict_prompt(plan, task, unresolved, cherry_error),
            mode="write",
            transient_retries=retries,
        )
        text, used = turn["text"], turn["model"]
        if "BLOCKED_NEEDS_USER:" in text:
            return False, text
        marker_files = []
        for rel in unresolved:
            path = Path(ctx["integration_dir"]) / rel
            if path.is_file():
                try:
                    text = path.read_text(errors="replace")
                    if "<<<<<<<" in text or ">>>>>>>" in text:
                        marker_files.append(rel)
                except Exception:
                    pass
        if marker_files:
            return False, "Orchestrator left conflict markers in: " + ", ".join(marker_files)
        git(ctx["integration_dir"], "add", "-A")
        unresolved_index = git(ctx["integration_dir"], "ls-files", "-u", check=False).stdout.strip()
        if unresolved_index:
            return False, "Orchestrator did not fully stage a conflict resolution"
        git(ctx["integration_dir"], "diff", "--cached", "--check")
        git(
            ctx["integration_dir"],
            "-c", "user.name=AgentDock",
            "-c", "user.email=agentdock@local",
            "-c", "core.editor=true",
            "cherry-pick", "--continue",
        )
        execute("UPDATE tasks SET status=?, error=?, integration_status=? WHERE id=?", ("done", "", "resolved_by_orchestrator", task["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} merge conflict resolved by {used}")
        return True, ""
    except Exception as e:
        return False, f"Orchestrator conflict recovery failed: {e}"

def integrate_write_result(plan, ctx, result):
    task = result["task"]
    if not result.get("ok"):
        return False
    commit_hash = result.get("commit") or ""
    if not commit_hash:
        execute("UPDATE tasks SET status=?, integration_status=? WHERE id=?", ("done", "no_changes", task["id"]))
        remove_worktree(ctx["repo_root"], result["wt"])
        delete_branch(ctx["repo_root"], result["branch"])
        return True
    try:
        git(ctx["integration_dir"], "-c", "user.name=AgentDock", "-c", "user.email=agentdock@local", "cherry-pick", commit_hash)
        execute("UPDATE tasks SET status=?, integration_status=? WHERE id=?", ("done", "integrated", task["id"]))
        remove_worktree(ctx["repo_root"], result["wt"])
        delete_branch(ctx["repo_root"], result["branch"])
        return True
    except Exception as e:
        recovered, detail = resolve_merge_conflict(plan, ctx, result, str(e))
        if recovered:
            remove_worktree(ctx["repo_root"], result["wt"])
            delete_branch(ctx["repo_root"], result["branch"])
            return True
        git(ctx["integration_dir"], "cherry-pick", "--abort", check=False)
        err = f"Parallel integration conflict could not be self-healed: {detail or e}"
        execute("UPDATE tasks SET status=?, error=?, integration_status=?, finished_at=? WHERE id=?", ("failed", err, "conflict", now(), task["id"]))
        return False


def mark_read_result_done(result):
    task = result["task"]
    if result.get("ok"):
        execute("UPDATE tasks SET status=?, integration_status=? WHERE id=?", ("done", "read_complete", task["id"]))
        return True
    return False


def ready_tasks(plan_id, pending):
    ready = []
    for seq, task in sorted(pending.items()):
        if task.get("status") in ("waiting_for_orchestrator", "waiting_for_user", "paused_by_user", "pausing", "resuming"):
            continue
        deps = json.loads(task.get("depends_json") or "[]")
        if not deps:
            ready.append(task)
            continue
        dep_states = [one("SELECT status FROM tasks WHERE plan_id=? AND seq=?", (plan_id, d)) for d in deps]
        if any(d and d["status"] in ("failed", "blocked", "cancelled") for d in dep_states):
            execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("blocked", "Dependency failed", now(), task["id"]))
            pending.pop(seq, None)
            continue
        if all(d and d["status"] == "done" for d in dep_states):
            ready.append(task)
    return ready


def resolve_waiting_consultations(plan, tasks, ctx=None):
    """Drain queued worker questions before launching a task a second time.

    A wave may produce more than one question. Only the first user-facing
    question is surfaced at once; the remaining consultation records stay
    durable and are resolved on the next resume instead of being rerun as
    ordinary worker tasks.
    """
    resolved_any = False
    for task in sorted(tasks or [], key=lambda item: item.get("seq", 0)):
        if plan_is_paused(plan["id"]):
            return {
                "ok": False,
                "paused": True,
                "deferred": True,
                "error": "Mission is paused; worker consultations remain queued.",
            }
        if task.get("status") != "waiting_for_orchestrator" or not task.get("consultation_id"):
            continue
        fresh = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
        resolution = resolve_worker_consultation(
            plan,
            fresh,
            result={
                "waiting_for_orchestrator": True,
                "consultation_id": fresh.get("consultation_id") or "",
            },
            ctx=ctx,
        )
        if resolution.get("paused"):
            return resolution
        if resolution.get("retry"):
            resolved_any = True
            continue
        return resolution
    return {"resolved": True} if resolved_any else {}


def synthesis_prompt(plan, task_rows):
    result_text = []
    for t in task_rows:
        result_text.append(
            f"TASK {t['seq']+1}: {t['title']}\nSTATUS: {t['status']}\nRESULT:\n{(t['output'] or t['error'])[-5000:]}"
        )
    return f"""You are the root orchestrator. The parallel worker phase has finished.

ORIGINAL GOAL:
{plan['goal']}

WORKER RESULTS:
{'\n\n'.join(result_text)}

Inspect the integrated workspace as needed in read-only mode. Produce a concise final synthesis for the user:
- what was accomplished,
- important implementation or research conclusions,
- checks that actually ran,
- unresolved failures/conflicts/risks,
- whether the integrated result is safe to apply.
Do not invent checks or results.
"""


def apply_integration_to_user_workspace(plan, ctx):
    repo_root = ctx["repo_root"]
    current = repo_info(plan["workspace"])
    if not current["is_git"] or current["head"] != ctx["base_commit"]:
        raise RuntimeError("Ana workspace HEAD plan çalışırken değişti; otomatik apply güvenli olmadığı için yapılmadı.")
    if current["dirty"]:
        raise RuntimeError("Ana workspace plan çalışırken değiştirildi; otomatik apply güvenli olmadığı için yapılmadı.")
    patch = git(ctx["integration_dir"], "diff", "--binary", f"{ctx['base_commit']}..HEAD").stdout
    if not patch.strip():
        return False
    git(repo_root, "apply", "--check", "-", input_text=patch)
    git(repo_root, "apply", "-", input_text=patch)
    return True


def cleanup_successful_plan(ctx, plan_id):
    base_dir, integration_dir = plan_paths(plan_id)
    remove_worktree(ctx["repo_root"], integration_dir)
    delete_branch(ctx["repo_root"], ctx["integration_branch"])
    if base_dir.exists():
        shutil.rmtree(base_dir, ignore_errors=True)
    git(ctx["repo_root"], "worktree", "prune", check=False)


def build_plan(plan_id):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        return
    try:
        if plan_is_paused(plan_id):
            log(orchestrator_log_id(plan_id), "supervisor", "mission planning is paused; waiting for an explicit resume")
            return
        # Capture repository/workspace facts before the model is asked to make
        # a disposition. This is read-only and is also shown as evidence.
        log(orchestrator_log_id(plan_id), "supervisor", "analyzing workspace before deciding whether tasks are needed")
        snapshot = workspace_snapshot(plan["workspace"])
        execute("UPDATE plans SET workspace_snapshot_json=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "capturing Codex quota baseline")
        start_usage = quota_status(force=True, wait=True)
        execute("UPDATE plans SET usage_start_json=? WHERE id=?", (json.dumps(start_usage), plan_id))
        agents = rows("SELECT * FROM agents ORDER BY created_at")
        prompt = planner_prompt(
            plan["goal"], agents, plan["worker_model"], plan["worker_effort"], plan["worker_tier"],
            plan["max_parallel"], snapshot=snapshot, planner_instruction=plan.get("replan_note") or "",
        )
        log(orchestrator_log_id(plan_id), "supervisor", "workspace analysis complete · disposition is now being decided")
        recovery = recovery_settings(plan)
        turn = run_mission_orchestrator_turn(
            plan_id,
            "initial_disposition",
            prompt,
            expected_output_schema=planner_schema_path(),
            mode="read",
            images=plan_attachment_paths(plan),
            transient_retries=1 if recovery.get("auto_retry_transient") else 0,
        )
        text, used_model = turn["text"], turn["model"]
        obj = normalize_planner_result(extract_json(text))
        if plan_is_paused(plan_id):
            log(orchestrator_log_id(plan_id), "supervisor", "mission was paused before the disposition could be materialized")
            write_mission_docs(plan_id)
            return
        mission_title = obj.get("title") or deterministic_mission_title(plan.get("goal"))
        items = obj["tasks"]
        valid_ids = {a["id"] for a in agents}
        disposition = obj["decision"]
        disposition_label = {
            "already_satisfied": "Already satisfied",
            "answer_only": "Answer only",
            "needs_user_input": "Waiting for user input",
            "blocked": "Blocked for safety or authority",
            "execute": "Execution required",
        }[disposition]
        record_control_event(plan_id, "agentdock.workspace", {
            "classification": snapshot.get("classification"),
            "repo_root": snapshot.get("repo_root"),
            "branch": snapshot.get("branch"),
            "upstream": snapshot.get("upstream"),
            "remote_names": [x.get("name") for x in snapshot.get("remotes") or []],
            "working_tree_clean": snapshot.get("working_tree_clean"),
        })
        log(orchestrator_log_id(plan_id), "supervisor", f"mission disposition · {disposition_label}")
        log(orchestrator_log_id(plan_id), "supervisor", f"reason · {obj['reason']}")
        for evidence in obj["evidence"]:
            log(orchestrator_log_id(plan_id), "supervisor", f"evidence · {evidence}")
        for question in obj["questions"]:
            log(orchestrator_log_id(plan_id), "supervisor", f"question · {question}")
        record_control_event(plan_id, "agentdock.disposition", {
            "decision": disposition,
            "reason": obj["reason"],
            "evidence": obj["evidence"],
            "final_response": obj["final_response"],
            "questions": obj["questions"],
            "task_count": len(items),
        })

        if disposition in NO_TASK_DECISIONS:
            status = "done" if disposition in ("already_satisfied", "answer_only") else ("waiting_for_user" if disposition == "needs_user_input" else "blocked")
            summary = obj["final_response"] or obj["reason"]
            error = obj["reason"] if disposition == "blocked" else ""
            finished = now() if status == "done" else None
            execute(
                "UPDATE plans SET title=?,decision=?,decision_reason=?,evidence_json=?,questions_json=?,final_response=?,summary=?,error=?,status=?,orchestrator_used=?,finished_at=? WHERE id=?",
                (
                    mission_title, disposition, obj["reason"], json.dumps(obj["evidence"], ensure_ascii=False),
                    json.dumps(obj["questions"], ensure_ascii=False), obj["final_response"], summary,
                    error, status, used_model, finished, plan_id,
                ),
            )
            log(orchestrator_log_id(plan_id), "supervisor", f"no execution needed · {len(items)} tasks created")
            write_mission_docs(plan_id)
            return

        for i, it in enumerate(items):
            agent_id = it.get("agent_id") if it.get("agent_id") in valid_ids else agents[0]["id"]
            deps = [d for d in it.get("depends_on", []) if isinstance(d, int) and 0 <= d < i]
            mode = it.get("mode", "read") if it.get("mode") in ("read", "write") else "read"
            contract = it.get("contract") if isinstance(it.get("contract"), dict) else {}
            execute(
                "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4())[:8], plan_id, i, it.get("title", f"Task {i+1}"),
                    contract.get("objective") or "Execute the orchestrator-defined task contract.",
                    agent_id, mode, json.dumps(deps), "pending", json.dumps(contract, ensure_ascii=False),
                ),
            )
            record_control_event(plan_id, "agentdock.task", {
                "seq": i,
                "title": it.get("title", f"Task {i+1}"),
                "agent_id": agent_id,
                "mode": mode,
                "depends_on": deps,
            })
            task_title = it.get("title") or f"Task {i+1}"
            dep_label = ",".join(str(d + 1) for d in deps) or "none"
            log(orchestrator_log_id(plan_id), "supervisor", f"TASK-{i+1:03d} · {task_title} · {mode} · deps={dep_label}")
        execute(
            "UPDATE plans SET title=?,decision=?,decision_reason=?,evidence_json=?,questions_json=?,final_response=?,status=?,orchestrator_used=?,error=?,finished_at=NULL WHERE id=?",
            (
                mission_title, disposition, obj["reason"], json.dumps(obj["evidence"], ensure_ascii=False),
                json.dumps(obj["questions"], ensure_ascii=False), obj["final_response"], "planned", used_model, "", plan_id,
            ),
        )
        log(orchestrator_log_id(plan_id), "supervisor", f"execution plan ready · {len(items)} task(s)")
        write_mission_docs(plan_id)
    except Exception as e:
        current_plan = one("SELECT * FROM plans WHERE id=?", (plan_id,)) or {}
        if current_plan.get("status") in ("paused", "pausing") or int(current_plan.get("paused") or 0):
            execute("UPDATE plans SET status=?,error='',finished_at=NULL WHERE id=?", ("paused", plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "mission planning paused; no new task graph was created")
        else:
            execute("UPDATE plans SET status=?, error=? WHERE id=?", ("attention", str(e), plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", f"planning failed: {e}")
        write_mission_docs(plan_id)


def replan_mission(plan_id, mode="reconsider", user_note=""):
    """Start a fresh disposition pass after an explicit user action."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    if plan.get("status") in ("planning", "preflight", "running"):
        raise ValueError("Mission zaten çalışıyor")
    if mode == "force_execute" and plan.get("decision") in ("blocked", "needs_user_input"):
        raise ValueError("Güvenlik veya eksik kullanıcı kararı varken execution planı zorlanamaz")
    instructions = {
        "force_execute": "The user explicitly requested an execution plan anyway. If no file change is needed, create the smallest honest read-only verification task rather than inventing a write.",
        "verify": "Run a fresh read-only verification of the original mission and workspace before deciding. Keep zero tasks if the outcome is already satisfied.",
        "reconsider": "Reconsider the mission disposition using fresh workspace evidence. Do not create tasks unless actual work is required.",
        "answer": "The user supplied a decision or answer. Re-evaluate the original mission with this answer and choose the correct disposition.",
    }
    note = instructions.get(mode, instructions["reconsider"])
    if user_note:
        note += "\nUser note:\n" + str(user_note).strip()[:6000]
    execute(
        "UPDATE consultations SET status=?,resolved_at=? WHERE plan_id=? AND status IN ('queued','resolving','waiting_for_user')",
        ("superseded", now(), plan_id),
    )
    execute(
        "DELETE FROM tasks WHERE plan_id=?",
        (plan_id,),
    )
    execute(
        "UPDATE plans SET status=?,decision=?,decision_reason=?,evidence_json=?,questions_json=?,final_response=?,summary=?,error=?,replan_note=?,finished_at=NULL,started_at=NULL,apply_status='',apply_error='' WHERE id=?",
        ("planning", "", "", "[]", "[]", "", "", "", note, plan_id),
    )
    log(orchestrator_log_id(plan_id), "supervisor", f"new disposition pass requested · {mode}")
    write_mission_docs(plan_id)
    threading.Thread(target=build_plan, args=(plan_id,), daemon=True).start()
    return {"ok": True, "status": "planning"}



def demo_event(session_id, task_id, plan_id, obj):
    """Persist a Codex-shaped event without calling Codex."""
    record_codex_event(session_id, task_id, plan_id, json.dumps(obj, ensure_ascii=False))


def demo_contract(goal, role, objective, allowed_paths, steps, acceptance, verification):
    return {
        "objective": objective,
        "context": f"Demo preview for mission: {goal}",
        "scope": {
            "in_scope": ["The requested mission outcome", "Only the focused demo task boundary"],
            "out_of_scope": ["Unrelated refactors", "Dependency or architecture changes not requested"],
        },
        "allowed_paths": allowed_paths,
        "required_inputs": ["Orchestrator execution contract", "Current workspace state"],
        "implementation_steps": steps,
        "acceptance_criteria": acceptance,
        "verification_commands": verification,
        "expected_output": [f"A concise {role.lower()} result", "Exact changed files or findings", "Verification status"],
        "escalation_conditions": ["Scope becomes ambiguous", "A public contract or architecture decision is required"],
        "decision_policy": "Implementation-level choices are allowed. Product, architecture and scope decisions remain with the orchestrator.",
    }


def insert_demo_task(plan_id, seq, title, agent_id, mode, deps, contract):
    tid = str(uuid.uuid4())[:8]
    execute(
        "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tid, plan_id, seq, title, contract.get("objective", title), agent_id, mode, json.dumps(deps), "pending", json.dumps(contract, ensure_ascii=False)),
    )
    return tid


def simulate_demo_task(plan, task_id, reasoning, command, file_changes=None, duration=1.4):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return
    agent = one("SELECT * FROM agents WHERE id=?", (task.get("agent_id"),)) or {}
    model = task_model(plan, agent, task)
    effort = task_effort(plan, agent, task)
    tier = task_tier(plan, agent, task)
    started = now()
    execute("UPDATE tasks SET status=?,started_at=?,error=? WHERE id=?", ("running", started, "", task_id))
    sid = create_agent_session(plan["id"], task_id, "demo-worker", model, effort, tier, task.get("mode") or "read", plan["workspace"])
    fake_thread = f"demo-{task_id}-{sid[:6]}"
    demo_event(sid, task_id, plan["id"], {"type":"thread.started","thread_id":fake_thread})
    execute("UPDATE tasks SET worker_thread_id=? WHERE id=?", (fake_thread, task_id))
    demo_event(sid, task_id, plan["id"], {"type":"turn.started","turn_id":f"demo-turn-{sid[:8]}"})
    log(task_id, "stdout", f"demo launch model={model} effort={effort} speed={tier}")
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"reason-1","type":"reasoning","text":reasoning}})
    demo_event(sid, task_id, plan["id"], {"type":"item.started","item":{"id":"todo-1","type":"todo_list","items":[
        {"text":"Inspect the assigned scope","completed":True},
        {"text":"Execute the focused task","completed":False},
        {"text":"Verify the result","completed":False},
    ]}})
    cmd_id = "cmd-1"
    demo_event(sid, task_id, plan["id"], {"type":"item.started","item":{"id":cmd_id,"type":"command_execution","command":command,"aggregated_output":"","status":"in_progress"}})
    log(task_id, "stdout", f"$ {command}")
    time.sleep(max(0.2, duration * 0.45))
    if (one("SELECT status FROM tasks WHERE id=?",(task_id,)) or {}).get("status") in ("cancelled", "paused_by_user") or plan_is_paused(plan["id"]):
        finish_agent_session(sid,"cancelled","User stopped demo agent")
        log(task_id,"manual","demo agent paused by user")
        return
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":cmd_id,"type":"command_execution","command":command,"aggregated_output":"demo command completed successfully\n","exit_code":0,"status":"completed"}})
    if file_changes:
        demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"files-1","type":"file_change","changes":file_changes,"status":"completed"}})
        for change in file_changes:
            log(task_id, "stdout", f"{change['kind']} {change['path']}")
    demo_event(sid, task_id, plan["id"], {"type":"item.updated","item":{"id":"todo-1","type":"todo_list","items":[
        {"text":"Inspect the assigned scope","completed":True},
        {"text":"Execute the focused task","completed":True},
        {"text":"Verify the result","completed":True},
    ]}})
    time.sleep(max(0.2, duration * 0.55))
    if (one("SELECT status FROM tasks WHERE id=?",(task_id,)) or {}).get("status") in ("cancelled", "paused_by_user") or plan_is_paused(plan["id"]):
        finish_agent_session(sid,"cancelled","User stopped demo agent")
        log(task_id,"manual","demo agent paused by user")
        return
    # Demo-mode queued steering: messages sent while Working become the next turn.
    for msg in queued_messages(task_id):
        execute("UPDATE task_messages SET status=? WHERE id=?", ("sending", msg["id"]))
        demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":f"user-steer-{msg['id']}","type":"agent_message","text":f"Queued user steering received for next turn: {msg.get('text') or '[attachment]'}"}})
        log(task_id, "manual", "demo queued message delivered on next turn")
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("delivered", "", msg["id"]))
    final = "Demo task completed. This is simulated activity for UI validation; no Codex quota was used."
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"msg-1","type":"agent_message","text":final}})
    demo_event(sid, task_id, plan["id"], {"type":"turn.completed","usage":{"input_tokens":0,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0}})
    finish_agent_session(sid, "completed", final)
    execute("UPDATE tasks SET status=?,output=?,finished_at=? WHERE id=?", ("done", final, now(), task_id))
    log(task_id, "stdout", "demo task complete")


def build_demo_plan(plan_id):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        return
    oid = orchestrator_log_id(plan_id)
    try:
        log(oid, "supervisor", "DEMO MODE · no Codex quota will be used")
        sid = create_agent_session(plan_id, oid, "demo-orchestrator", plan["orchestrator_model"], plan["orchestrator_effort"], plan["orchestrator_tier"], "read", plan["workspace"])
        demo_event(sid, oid, plan_id, {"type":"thread.started","thread_id":f"demo-orchestrator-{plan_id}"})
        demo_event(sid, oid, plan_id, {"type":"turn.started","turn_id":f"demo-orchestrator-turn-{plan_id}"})
        demo_event(sid, oid, plan_id, {"type":"item.completed","item":{"id":"orch-r1","type":"reasoning","text":"I will propose explicit task boundaries and dependencies first. Execution will not start until the user reviews assignments and approves the plan."}})
        log(oid, "supervisor", "building preview execution contracts")
        time.sleep(0.55)
        goal = plan["goal"]
        insert_demo_task(plan_id, 0, "Inspect scope and affected surfaces", "architect", "read", [], demo_contract(goal, "Architect", "Inspect the requested outcome, identify likely affected surfaces and define safe implementation boundaries.", ["workspace/** (read-only)"], ["Inspect repository structure", "Locate likely affected files", "Summarize constraints for workers"], ["Affected surfaces are explicit", "No implementation changes are made"], ["git status --short", "rg relevant symbols"]))
        insert_demo_task(plan_id, 1, "Implement the primary change", "coder", "write", [0], demo_contract(goal, "Coder", "Implement the primary requested change inside the orchestrator-defined scope.", ["src/**", "tests/**"], ["Read architect findings", "Apply the smallest focused change", "Run targeted verification"], ["Requested behavior is implemented", "Diff stays focused", "Targeted checks pass"], ["git diff --check", "targeted test command"]))
        insert_demo_task(plan_id, 2, "Add focused verification", "tester", "write", [0], demo_contract(goal, "Tester", "Add or update the smallest meaningful verification for the requested outcome.", ["tests/**", "src/** only when test fixtures require it"], ["Identify the regression boundary", "Add focused coverage", "Run the relevant test slice"], ["Coverage demonstrates the requested behavior", "Verification passes"], ["targeted test command"]))
        insert_demo_task(plan_id, 3, "Review the integrated result", "reviewer", "read", [1,2], demo_contract(goal, "Reviewer", "Independently review the integrated result for scope compliance, regressions and missing verification.", ["workspace/** (read-only)"], ["Inspect integrated diff", "Check acceptance criteria", "Report residual risks"], ["No critical regression is found", "Verification result is explicit"], ["git diff --check", "test summary"]))
        execute("UPDATE plans SET title=?,status=?,decision=?,decision_reason=?,evidence_json=?,final_response=?,orchestrator_used=? WHERE id=?", (deterministic_mission_title(goal), "planned", "execute", "Demo preview intentionally exercises the execution path.", json.dumps(["Local demo simulator selected; no Codex worker was called."], ensure_ascii=False), "", plan["orchestrator_model"], plan_id))
        record_control_event(plan_id, "agentdock.disposition", {"decision": "execute", "reason": "Demo preview intentionally exercises the execution path.", "evidence": ["Local demo simulator selected; no Codex worker was called."], "task_count": 4})
        demo_event(sid, oid, plan_id, {"type":"item.completed","item":{"id":"orch-plan","type":"todo_list","items":[{"text":"Define safe task boundaries","completed":True},{"text":"Wait for user plan review","completed":False},{"text":"Execute approved task graph","completed":False}]}})
        demo_event(sid, oid, plan_id, {"type":"item.completed","item":{"id":"orch-msg","type":"agent_message","text":"Preview plan ready. Review task scope, dependencies, and agent assignments before starting execution."}})
        demo_event(sid, oid, plan_id, {"type":"turn.completed","usage":{"input_tokens":0,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0}})
        finish_agent_session(sid, "completed", "Demo plan ready for review")
        log(oid, "supervisor", "preview plan ready · waiting for user approval")
        write_mission_docs(plan_id)
    except Exception as e:
        execute("UPDATE plans SET status=?,error=? WHERE id=?", ("attention", f"Demo simulator error: {e}", plan_id))
        log(oid, "supervisor", f"demo simulator failed: {e}")
        write_mission_docs(plan_id)


def run_demo_execution(plan_id):
    plan=one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        return
    if plan.get('status') not in ('approved','attention'):
        raise ValueError('Demo planını önce onayla.')
    oid=orchestrator_log_id(plan_id)
    try:
        execute("UPDATE plans SET status=?,error=?,started_at=?,finished_at=NULL,apply_status=?,apply_error=? WHERE id=?", ('running','',now(),'','',plan_id))
        tasks=rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
        byseq={t['seq']:t for t in tasks}
        log(oid,'supervisor','user approved plan · starting demo execution')
        # task 1
        simulate_demo_task(plan, byseq[0]['id'], "I am mapping the requested outcome to a narrow file and behavior boundary before implementation begins.", "rg --files | head -40", duration=2.0)
        if plan_is_paused(plan_id):
            write_mission_docs(plan_id)
            return
        log(oid, "supervisor", "launching approved parallel preview wave: TASK-002 + TASK-003")
        th1=threading.Thread(target=simulate_demo_task,args=(plan,byseq[1]['id'],"The contract is explicit, so I can implement the focused change without making product or architecture decisions.","git diff --check"),kwargs={"file_changes":[{"path":"src/example.py","kind":"update"}],"duration":4.0},daemon=True)
        th2=threading.Thread(target=simulate_demo_task,args=(plan,byseq[2]['id'],"I am adding the smallest regression check that proves the requested behavior while avoiding unrelated coverage expansion.","python3 -m unittest discover -s tests"),kwargs={"file_changes":[{"path":"tests/test_example.py","kind":"add"}],"duration":4.0},daemon=True)
        th1.start(); th2.start(); th1.join(); th2.join()
        if plan_is_paused(plan_id):
            write_mission_docs(plan_id)
            return
        log(oid,'supervisor','parallel wave complete · starting independent review')
        simulate_demo_task(plan,byseq[3]['id'],"I am reviewing the simulated integrated result against the execution contracts and verification evidence.","git diff --check && python3 -m unittest discover -s tests",duration=2.5)
        if plan_is_paused(plan_id):
            write_mission_docs(plan_id)
            return
        final_tasks=rows("SELECT status FROM tasks WHERE plan_id=?",(plan_id,))
        all_done=bool(final_tasks) and all(t['status']=='done' for t in final_tasks)
        summary='Demo preview completed successfully. All activity was simulated locally and used no Codex quota.' if all_done else 'Demo mission stopped with one or more tasks requiring attention.'
        execute("UPDATE plans SET status=?,summary=?,usage_start_json=?,usage_end_json=?,finished_at=? WHERE id=?", ('done' if all_done else 'attention',summary,'{}','{}',now(),plan_id))
        log(oid,'supervisor','DEMO MODE · mission '+('complete' if all_done else 'needs attention')+' · 0 quota used')
        write_mission_docs(plan_id)
    except Exception as e:
        execute("UPDATE plans SET status=?,error=? WHERE id=?", ('attention',str(e),plan_id))
        log(oid,'supervisor',f'demo execution failed: {e}')
        write_mission_docs(plan_id)


def demo_task_diff(task):
    seq = int(task.get("seq") or 0)
    if seq == 1:
        return """diff --git a/src/example.py b/src/example.py\nindex 41f9abc..0a71def 100644\n--- a/src/example.py\n+++ b/src/example.py\n@@ -8,5 +8,9 @@ def handle_request(value):\n-    return value\n+    # Demo preview only — no file was changed on disk.\n+    normalized = value.strip()\n+    if not normalized:\n+        return None\n+    return normalized\n"""
    if seq == 2:
        return """diff --git a/tests/test_example.py b/tests/test_example.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_example.py\n@@ -0,0 +1,6 @@\n+def test_demo_behavior():\n+    # Demo preview only — no file was created on disk.\n+    assert True\n"""
    return "Demo preview: this task is read-only, so there is no file diff."


def run_demo_manual_followup(task_id, prompt, image_paths=None):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],)) if task else None
    if not task or not plan:
        return
    existing_thread = task.get("worker_thread_id") or (latest_agent_session(task_id) or {}).get("thread_id") or ""
    sid = create_agent_session(plan["id"], task_id, "demo-manual", plan["worker_model"], plan["worker_effort"], plan["worker_tier"], task.get("mode") or "read", plan["workspace"])
    thread_id = existing_thread or f"demo-followup-{task_id}-{sid[:6]}"
    demo_event(sid, task_id, plan["id"], {"type":"thread.started","thread_id":thread_id})
    demo_event(sid, task_id, plan["id"], {"type":"turn.started","turn_id":f"demo-manual-turn-{sid[:8]}"})
    log(task_id, "manual", f"demo manual input: {prompt or '[attachment only]'}")
    time.sleep(0.4)
    note = "I received your manual instruction in Demo Mode and continued the same worker conversation while preserving the task contract."
    if image_paths:
        note += f" {len(image_paths)} image attachment(s) were accepted."
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"manual-reason","type":"reasoning","text":"The user has taken manual control, so I will prioritize the new instruction over the original execution preference while keeping the task contract boundaries."}})
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"manual-msg","type":"agent_message","text":note}})
    demo_event(sid, task_id, plan["id"], {"type":"turn.completed","usage":{"input_tokens":0,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0}})
    finish_agent_session(sid, "completed", note)
    log(task_id, "manual", "demo manual follow-up completed · 0 quota used")
    return note


def worker_consultation_prompt(plan, task, consultation, user_answer=None):
    contract = safe_json(task.get("contract_json"), {})
    answer_block = ""
    if user_answer:
        answer_block = f"""
USER ANSWER TO THE WORKER'S QUESTION:
{json.dumps(user_answer, ensure_ascii=False, indent=2)}

Use this answer only for the unresolved decision. Do not ask the same question
again unless the answer is genuinely insufficient and explain why.
"""
    return f"""You are the single root orchestrator for this mission. A worker
paused because it reached a product, architecture, factual, legal, scope or
destructive-operation decision that it is not allowed to invent.

MISSION:
{plan['goal']}

TASK:
TASK-{task['seq']+1:03d} — {task['title']}

CURRENT TASK CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

WORKER THREAD:
{task.get('worker_thread_id') or consultation.get('worker_thread_id') or '[not recorded]'}

WORKER QUESTION:
{consultation.get('question') or ''}

WHY THE WORKER STOPPED:
{consultation.get('reason') or ''}

EVIDENCE:
{json.dumps(safe_json(consultation.get('evidence_json'), consultation.get('evidence') or []), ensure_ascii=False, indent=2)}

OPTIONS REPORTED BY WORKER:
{json.dumps(safe_json(consultation.get('options_json'), consultation.get('options') or []), ensure_ascii=False, indent=2)}
{answer_block}

Return ONLY this JSON shape:
{json.dumps(CONSULTATION_SCHEMA, ensure_ascii=False, indent=2)}

Choose exactly one action:
- answer_worker: give a concrete bounded instruction without changing the contract.
- revise_contract: give a complete revised contract and the worker instruction.
- ask_user: ask the user for missing product/factual information; do not guess.
- block_mission: the requested work is unsafe, unauthorized or impossible to decide.

Never create a new task or a new orchestrator thread in this turn.
"""


def _consultation_payload(row):
    row = row or {}
    return {
        "id": row.get("id") or "",
        "question": row.get("question") or "",
        "reason": row.get("reason") or "",
        "evidence": safe_json(row.get("evidence_json"), []),
        "options": safe_json(row.get("options_json"), []),
        "worker_thread_id": row.get("worker_thread_id") or "",
        "status": row.get("status") or "",
        "task_id": row.get("task_id") or "",
    }


def _defer_consultation_for_paused_plan(plan, task, consultation, payload=None):
    """Keep a worker question durable when a mission pause interrupts resolution."""
    payload = payload or _consultation_payload(consultation)
    question = (
        payload.get("question")
        or task.get("waiting_reason")
        or "Worker consultation is waiting for the orchestrator."
    )
    execute(
        """UPDATE tasks SET status=?,error=?,waiting_reason=?,finished_at=NULL
           WHERE id=?""",
        ("waiting_for_orchestrator", "", question, task["id"]),
    )
    execute(
        """UPDATE consultations SET status=?,orchestrator_response_json=?,resolved_at=NULL
           WHERE id=?""",
        ("queued", "{}", consultation["id"]),
    )
    log(
        orchestrator_log_id(plan["id"]),
        "supervisor",
        f"TASK-{task['seq']+1:03d} consultation kept queued because the mission is paused",
    )
    write_mission_docs(plan["id"])
    return {
        "ok": False,
        "paused": True,
        "deferred": True,
        "error": "Mission is paused; worker consultation remains queued.",
    }


def _set_pending_consultation(plan_id, task, consultation, response):
    questions = response.get("questions") or [consultation.get("question") or "Additional information is required."]
    pending = {
        "kind": "execution_question",
        "consultation_id": consultation["id"],
        "task_id": task["id"],
        "task_title": task["title"],
        "question": questions[0],
        "questions": questions,
        "reason": response.get("reason") or consultation.get("reason") or "",
        "evidence": consultation.get("evidence") or [],
        "options": consultation.get("options") or [],
        "orchestrator_evidence": response.get("evidence") or [],
        "worker_thread_id": task.get("worker_thread_id") or consultation.get("worker_thread_id") or "",
        "orchestrator_thread_id": "",
    }
    execute(
        """UPDATE consultations SET status=?,orchestrator_response_json=?,
           user_questions_json=? WHERE id=?""",
        ("waiting_for_user", json.dumps(response, ensure_ascii=False), json.dumps(questions, ensure_ascii=False), consultation["id"]),
    )
    execute(
        """UPDATE tasks SET status=?,waiting_reason=?,consultation_id=? WHERE id=?""",
        ("waiting_for_user", pending["question"], consultation["id"], task["id"]),
    )
    execute(
        """UPDATE plans SET status=?,pending_question_id=?,pending_question_json=?,
           error=?,finished_at=NULL WHERE id=?""",
        ("waiting_for_user", consultation["id"], json.dumps(pending, ensure_ascii=False), response.get("reason") or "", plan_id),
    )
    return pending


def worker_resume_message(response, contract):
    """Turn an orchestrator decision into an explicit same-thread worker handoff."""
    return (
        "ROOT ORCHESTRATOR DECISION\n\n"
        f"{response.get('worker_message') or response.get('reason') or 'Continue the bounded task.'}\n\n"
        "Updated contract:\n"
        f"{json.dumps(contract or {}, ensure_ascii=False, indent=2)}\n\n"
        "Continue the same task from your current state. Preserve the prior context and tool findings."
    )


def same_worker_resume_handoff(task, instruction="Continue the same task from your last durable checkpoint."):
    """Build the durable context sent when a worker continues its own thread."""
    thread_id = str(task.get("worker_thread_id") or "").strip()
    if not thread_id:
        thread_id = str((latest_agent_session(task.get("id")) or {}).get("thread_id") or "").strip()
    checkpoint = str(task.get("output") or "").strip()[-6000:]
    return (
        f"{instruction}\n"
        f"Existing worker thread: {thread_id or 'not yet bound'}\n"
        f"Last checkpoint/output:\n{checkpoint or 'No textual checkpoint was recorded.'}\n"
        "Do not create a new session and do not redo completed work."
    )


def resolve_worker_consultation(plan, task, result=None, ctx=None, user_answer=None):
    """Ask the same orchestrator thread to resolve a worker's structured question."""
    fresh = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
    consultation_id = fresh.get("consultation_id") or (result or {}).get("consultation_id") or ""
    consultation = one("SELECT * FROM consultations WHERE id=?", (consultation_id,)) if consultation_id else None
    if not consultation and result:
        request = result.get("consultation") or extract_worker_consultation(result.get("output") or "")
        if request:
            consultation = create_worker_consultation(plan, fresh, result.get("output") or "")
    if not consultation:
        return {"ok": False, "error": "Worker consultation record not found."}
    payload = _consultation_payload(consultation)
    if plan_is_paused(plan["id"]):
        return _defer_consultation_for_paused_plan(plan, fresh, consultation, payload)
    # Keep a write worker's isolated worktree alive while the orchestrator is
    # deciding. The worker may have uncommitted progress and must resume that
    # exact checkout after the handoff; cleanup belongs to successful
    # integration or an explicit terminal cleanup path, never to a
    # consultation boundary.
    purpose = "user_answer" if user_answer else "worker_consultation"
    answer_images = []
    if isinstance(user_answer, dict):
        answer_images = [
            str(Path(path).expanduser().resolve())
            for path in (user_answer.get("attachments") or [])
            if Path(str(path)).expanduser().is_file()
        ][:8]
    log(
        orchestrator_log_id(plan["id"]),
        "supervisor",
        f"TASK-{fresh['seq']+1:03d} consultation → orchestrator · purpose={purpose}",
    )
    try:
        turn = run_mission_orchestrator_turn(
            plan["id"],
            purpose,
            worker_consultation_prompt(plan, fresh, payload, user_answer=user_answer),
            expected_output_schema=consultation_schema_path(),
            mode="read",
            images=answer_images,
            transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
        )
        if plan_is_paused(plan["id"]):
            return _defer_consultation_for_paused_plan(plan, fresh, consultation, payload)
        response = normalize_consultation_result(extract_json(turn["text"]))
        response["orchestrator_thread_id"] = turn.get("thread_id") or ""
        execute(
            "UPDATE consultations SET orchestrator_response_json=?,orchestrator_thread_id=? WHERE id=?",
            (json.dumps(response, ensure_ascii=False), response["orchestrator_thread_id"], consultation["id"]),
        )
        if response["action"] in ("answer_worker", "revise_contract"):
            contract = response["revised_contract"] if response["action"] == "revise_contract" else safe_json(fresh.get("contract_json"), {})
            execute(
                """UPDATE tasks SET status=?,contract_json=?,instructions=?,error=?,
                   waiting_reason='',consultation_id='',worker_resume_message=?,finished_at=NULL WHERE id=?""",
                (
                    "pending",
                    json.dumps(contract, ensure_ascii=False),
                    contract.get("objective") or fresh.get("instructions") or response["worker_message"],
                    "",
                    worker_resume_message(response, contract),
                    fresh["id"],
                ),
            )
            execute(
                "UPDATE consultations SET status=?,resolved_at=? WHERE id=?",
                ("resolved", now(), consultation["id"]),
            )
            execute(
                "UPDATE plans SET pending_question_id=?,pending_question_json=?,error=?,status=? WHERE id=?",
                ("", "{}", "", "running", plan["id"]),
            )
            log(
                orchestrator_log_id(plan["id"]),
                "supervisor",
                f"TASK-{fresh['seq']+1:03d} decision received · same worker thread will resume",
            )
            record_control_event(plan["id"], "agentdock.worker_resume", {
                "task_id": fresh["id"],
                "consultation_id": consultation["id"],
                "worker_thread_id": fresh.get("worker_thread_id") or payload.get("worker_thread_id") or "",
                "orchestrator_thread_id": response["orchestrator_thread_id"],
                "message": response["worker_message"],
            }, task_id=fresh["id"])
            write_mission_docs(plan["id"])
            return {
                "ok": True,
                "retry": True,
                "worker_message": response["worker_message"],
                "task": one("SELECT * FROM tasks WHERE id=?", (fresh["id"],)),
            }
        if response["action"] == "ask_user":
            pending = _set_pending_consultation(plan["id"], fresh, payload, response)
            pending["orchestrator_thread_id"] = response["orchestrator_thread_id"]
            execute(
                "UPDATE plans SET pending_question_json=? WHERE id=?",
                (json.dumps(pending, ensure_ascii=False), plan["id"]),
            )
            log(orchestrator_log_id(plan["id"]), "supervisor", "mission paused · worker requires user information")
            write_mission_docs(plan["id"])
            return {"ok": False, "waiting_for_user": True, "pending": pending}
        reason = response["reason"] or "Orchestrator blocked the mission."
        execute(
            "UPDATE consultations SET status=?,resolved_at=? WHERE id=?",
            ("blocked", now(), consultation["id"]),
        )
        execute(
            "UPDATE tasks SET status=?,error=?,waiting_reason=? WHERE id=?",
            ("blocked", reason, "", fresh["id"]),
        )
        execute(
            "UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?",
            ("blocked", reason, now(), plan["id"]),
        )
        log(orchestrator_log_id(plan["id"]), "supervisor", f"mission blocked by orchestrator decision · {reason}")
        write_mission_docs(plan["id"])
        return {"ok": False, "blocked": True, "error": reason}
    except Exception as exc:
        message = f"Worker consultation could not be resolved: {exc}"
        if plan_is_paused(plan["id"]):
            return _defer_consultation_for_paused_plan(plan, fresh, consultation, payload)
        execute(
            "UPDATE tasks SET status=?,error=?,waiting_reason=? WHERE id=?",
            ("attention", message, payload.get("question") or "", fresh["id"]),
        )
        execute("UPDATE consultations SET status=?,orchestrator_response_json=? WHERE id=?",
                ("failed", json.dumps({"error": message}, ensure_ascii=False), consultation["id"]))
        execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("attention", message, plan["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", message)
        write_mission_docs(plan["id"])
        return {"ok": False, "attention": True, "error": message}


def escalation_prompt(plan, task, worker_output):
    contract = safe_json(task.get("contract_json"), {})
    return f"""You are the root orchestrator supervising a smaller worker. The worker correctly refused to make a reserved decision.

PARENT GOAL:
{plan['goal']}

TASK:
{task['title']}

CURRENT CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

WORKER ESCALATION:
{worker_output}

DEPENDENCY RESULTS:
{task_dependency_context(task) or 'None.'}

Inspect the integrated workspace if needed. Make the missing architecture/scope decision yourself.
Return ONLY JSON in one of these forms:
{{"action":"retry","note":"decision made by orchestrator","contract":{{...complete revised execution contract...}}}}
or
{{"action":"stop","reason":"why the mission cannot safely decide this automatically"}}

Rules:
- Never delegate the same unresolved decision back to the worker.
- For retry, return the COMPLETE revised contract, not a patch.
- Keep scope as narrow as possible.
- Preserve explicit acceptance criteria and verification.
"""


def resolve_worker_escalation(plan, task, result, workspace, ctx=None):
    fresh = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
    count = int(fresh.get("escalation_count") or 0)
    if count >= 2:
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} escalation retry limit reached")
        return False
    log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} escalated a reserved decision; root orchestrator is resolving it")
    try:
        if not fresh.get("consultation_id"):
            created = create_worker_consultation(
                plan,
                fresh,
                result.get("output") or result.get("error") or "Worker needs an orchestrator decision.",
            )
            if created:
                fresh = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or fresh
        resolved = resolve_worker_consultation(plan, fresh, result=result, ctx=ctx)
        if resolved.get("retry"):
            execute("UPDATE tasks SET escalation_count=? WHERE id=?", (count + 1, task["id"]))
            return True
        return False
    except Exception as e:
        execute("UPDATE tasks SET status=?, error=?, escalation_count=? WHERE id=?", ("blocked", f"Orchestrator escalation resolution failed: {e}", count + 1, task["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"escalation resolution failed: {e}")
        write_mission_docs(plan["id"])
        return False



def failure_recovery_prompt(plan, task, result):
    contract = safe_json(task.get("contract_json"), {})
    return f"""You are the root orchestrator supervising a smaller execution worker. The worker failed while executing an already-decided task contract.

MISSION:
{plan['goal']}

TASK:
TASK-{task['seq']+1:03d} — {task['title']}

CURRENT CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

WORKER FAILURE:
{(result.get('error') or result.get('output') or '')[-6000:]}

DEPENDENCY RESULTS:
{task_dependency_context(task) or 'None.'}

Diagnose only whether the worker can succeed with a more explicit version of the SAME task contract.
Return ONLY the consultation JSON object with action `revise_contract` or `block_mission`.
For revise_contract, include the COMPLETE revised contract in `revised_contract` and
a concrete `worker_message`. For block_mission, explain why automatic recovery is unsafe.

Rules:
- Do not broaden scope, change product behavior, add dependencies, alter public APIs, or make destructive changes.
- A revised contract must be complete and more concrete: exact steps, paths, acceptance criteria and verification.
- Preserve the original task objective unless the failure proves it impossible.
- If a user/product/architecture decision is required, stop rather than guessing.
"""


def resolve_worker_failure(plan, task, result, ctx=None):
    if result.get("phase") != "worker" or result.get("blocked"):
        return False
    fresh = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
    count = int(fresh.get("repair_count") or 0)
    if count >= 1:
        return False
    worker_thread_id = str(fresh.get("worker_thread_id") or (latest_agent_session(fresh["id"]) or {}).get("thread_id") or "").strip()
    if fresh.get("worker_resume_message") and not worker_thread_id:
        message = "Worker conversation could not be resumed; explicit task recovery is required."
        execute("UPDATE tasks SET status=?,error=?,repair_count=? WHERE id=?", ("attention", message, count + 1, fresh["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} recovery stopped without opening a new worker session")
        write_mission_docs(plan["id"])
        return False
    # Preserve the worker checkout and thread while the orchestrator diagnoses
    # a bounded failure. If the contract is revised, run_parallel_task will
    # reuse this task's worktree and resume the same Codex conversation.
    log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} failed; root orchestrator is diagnosing one bounded recovery attempt")
    execute("UPDATE plans SET recovery_count=recovery_count+1 WHERE id=?", (plan["id"],))
    try:
        turn = run_mission_orchestrator_turn(
            plan["id"],
            "failure_recovery",
            failure_recovery_prompt(plan, fresh, result),
            expected_output_schema=consultation_schema_path(),
            mode="read",
            transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
        )
        obj = normalize_consultation_result(extract_json(turn["text"]))
        used = turn.get("model") or plan.get("orchestrator_used") or plan["orchestrator_model"]
        if obj.get("action") in ("revise_contract", "answer_worker") and isinstance(obj.get("revised_contract"), dict) and obj.get("revised_contract"):
            contract = obj["revised_contract"]
            execute(
                "UPDATE tasks SET contract_json=?, instructions=?, status='pending', error='', output='', started_at=NULL, finished_at=NULL, repair_count=?, worker_thread_id=?, worker_resume_message=? WHERE id=?",
                (
                    json.dumps(contract, ensure_ascii=False),
                    contract.get("objective") or fresh.get("instructions") or "",
                    count + 1,
                    worker_thread_id,
                    same_worker_resume_handoff(fresh, "The previous worker turn failed. Continue the same task after applying this clarified contract."),
                    task["id"],
                ),
            )
            log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} recovery contract revised by {used}; worker will retry once")
            write_mission_docs(plan["id"])
            return True
        reason = obj.get("reason") or "Orchestrator found no safe automatic recovery."
        execute("UPDATE tasks SET repair_count=?, error=? WHERE id=?", (count + 1, reason, task["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} automatic recovery stopped: {reason}")
        write_mission_docs(plan["id"])
        return False
    except Exception as e:
        execute("UPDATE tasks SET repair_count=?, error=? WHERE id=?", (count + 1, f"Recovery diagnosis failed: {e}", task["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} recovery diagnosis failed: {e}")
        write_mission_docs(plan["id"])
        return False

def stored_integration_context(plan):
    """Rebuild the persisted integration worktree context after a restart."""
    info = repo_info(plan["workspace"])
    integration_dir = plan_paths(plan["id"])[1]
    integration_workspace = Path(plan.get("integration_workspace") or "").expanduser().resolve()
    if not info.get("is_git") or not plan.get("base_commit") or not integration_workspace.is_dir():
        raise RuntimeError("Mission integration worktree artık mevcut değil; mission yeniden çalıştırılmalı.")
    try:
        integration_workspace.relative_to(integration_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("Persisted integration workspace güvenli sınırın dışında.") from exc
    return {
        "repo_root": Path(info["root"]),
        "base_commit": plan["base_commit"],
        "workspace_rel": integration_workspace.relative_to(integration_dir.resolve()),
        "integration_dir": integration_dir.resolve(),
        "integration_workspace": integration_workspace,
        "integration_branch": f"agentdock/{plan['id']}/integration",
    }


def integration_patch(ctx):
    result = git(
        ctx["integration_dir"],
        "diff", "--binary", f"{ctx['base_commit']}..HEAD", check=False,
    )
    return result.stdout or result.stderr


def apply_plan(plan_id):
    """Apply a reviewed integration diff to the user's unchanged workspace."""
    if not claim_plan_run(plan_id):
        raise RuntimeError("Bu mission için başka bir işlem zaten çalışıyor.")
    try:
        plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
        if not plan:
            raise ValueError("Mission bulunamadı")
        if plan.get("status") != "awaiting_apply":
            raise ValueError("Mission apply için hazır değil")
        tasks = rows("SELECT status FROM tasks WHERE plan_id=?", (plan_id,))
        if not tasks or not all(t["status"] == "done" for t in tasks):
            raise ValueError("Tüm task'lar tamamlanmadan apply yapılamaz")
        ctx = stored_integration_context(plan)
        execute("UPDATE plans SET apply_status=?, apply_error=? WHERE id=?", ("checking", "", plan_id))
        try:
            run_preflight(plan, True, phase="apply")
        except PreflightWaitingForUser as exc:
            execute("UPDATE plans SET status=?,apply_status=?,apply_error=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", "waiting_for_user", str(exc), str(exc), plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "apply paused · waiting for an explicit preflight choice")
            write_mission_docs(plan_id)
            return {"ok": False, "waiting_for_user": True, "status": "waiting_for_user", "report": exc.report}
        except PreflightBlocked as exc:
            execute("UPDATE plans SET status=?,apply_status=?,apply_error=?,error=?,finished_at=? WHERE id=?", ("blocked", "blocked", str(exc), str(exc), now(), plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "apply stopped by a safety preflight blocker")
            write_mission_docs(plan_id)
            return {"ok": False, "blocked": True, "status": "blocked", "report": exc.report}
        patch = integration_patch(ctx)
        if not patch.strip():
            execute(
                "UPDATE plans SET status=?, applied=0, apply_status=?, apply_error=?, finished_at=? WHERE id=?",
                ("done", "no_changes", "", now(), plan_id),
            )
            cleanup_successful_plan(ctx, plan_id)
            log(orchestrator_log_id(plan_id), "supervisor", "no integration diff; mission completed without workspace changes")
            write_mission_docs(plan_id)
            return {"ok": True, "applied": False}
        applied = apply_integration_to_user_workspace(plan, ctx)
        execute(
            "UPDATE plans SET status=?, applied=?, apply_status=?, apply_error=?, finished_at=? WHERE id=?",
            ("done", 1 if applied else 0, "applied" if applied else "no_changes", "", now(), plan_id),
        )
        cleanup_successful_plan(ctx, plan_id)
        log(orchestrator_log_id(plan_id), "supervisor", "reviewed integration diff applied to user workspace")
        write_mission_docs(plan_id)
        return {"ok": True, "applied": bool(applied)}
    except Exception as exc:
        message = str(exc)
        execute(
            "UPDATE plans SET status=?, apply_status=?, apply_error=?, error=?, finished_at=? WHERE id=?",
            ("attention", "failed", message, message, now(), plan_id),
        )
        log(orchestrator_log_id(plan_id), "supervisor", f"apply requires attention: {message}")
        write_mission_docs(plan_id)
        raise
    finally:
        release_plan_run(plan_id)


def run_plan(plan_id, claimed=False, read_only_only=False):
    if not claimed and not claim_plan_run(plan_id):
        log(orchestrator_log_id(plan_id), "supervisor", "duplicate run request ignored; another run is active")
        return
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        release_plan_run(plan_id)
        return
    try:
        if plan.get("status") in ("paused", "pausing") or int(plan.get("paused") or 0):
            log(orchestrator_log_id(plan_id), "supervisor", "mission is paused; execution will resume only after an explicit user action")
            return
        if int(plan.get("demo_mode") or 0):
            return run_demo_execution(plan_id)
        if plan.get("status") not in ("approved", "attention", "waiting_for_user"):
            raise ValueError("Planı önce Plan Review ekranından onayla.")
        all_tasks = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
        if not all_tasks:
            # A disposition with zero tasks is a completed answer, a user
            # question, or a safety block. It must never enter execution
            # preflight or create a misleading worker queue.
            log(orchestrator_log_id(plan_id), "supervisor", "no tasks in mission; execution preflight skipped")
            write_mission_docs(plan_id)
            return
        if plan.get("status") == "waiting_for_user" and plan.get("pending_question_id"):
            log(orchestrator_log_id(plan_id), "supervisor", "mission is waiting for a persisted user answer; execution remains paused")
            write_mission_docs(plan_id)
            return
        tasks = all_tasks
        # A worker resume is a short-lived control state. Once the mission
        # runner owns the next turn, make it schedulable again while keeping
        # the persisted worker_thread_id and resume handoff intact.
        if any(t.get("status") == "resuming" for t in tasks):
            execute(
                "UPDATE tasks SET status='pending',error='',finished_at=NULL WHERE plan_id=? AND status='resuming'",
                (plan_id,),
            )
            all_tasks = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
            tasks = all_tasks
        if read_only_only:
            # A read-only continuation may run a safe read subgraph, but must
            # not pretend that write tasks (or reads depending on writes) are
            # complete. Remove every task that depends, directly or
            # transitively, on a write task.
            read_seqs = {t["seq"] for t in all_tasks if t.get("mode") == "read"}
            changed = True
            while changed:
                changed = False
                for task in all_tasks:
                    if task["seq"] not in read_seqs:
                        continue
                    dependencies = json.loads(task.get("depends_json") or "[]")
                    if any(dep not in read_seqs for dep in dependencies):
                        read_seqs.remove(task["seq"])
                        changed = True
            tasks = [t for t in all_tasks if t["seq"] in read_seqs]
            if not tasks:
                execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", "No independent read-only task can run until the write-task dependency is resolved.", plan_id))
                log(orchestrator_log_id(plan_id), "supervisor", "read-only continuation paused; all read tasks depend on write work")
                write_mission_docs(plan_id)
                return
            log(orchestrator_log_id(plan_id), "supervisor", f"read-only continuation selected {len(tasks)} task(s); write tasks remain paused")
        has_write = any(t["mode"] == "write" for t in tasks)
        ctx = None
        preserve_restart_checkpoint = bool(
            plan.get("status") == "attention" and int(plan.get("restart_recovery_pending") or 0)
        )
        if plan.get("status") == "attention" and not read_only_only:
            reset_plan_for_retry(plan, preserve_completed=preserve_restart_checkpoint)
            plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
            tasks = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
            has_write = any(t["mode"] == "write" for t in tasks)
        execute("UPDATE plans SET status=?, error=?, summary=?, applied=?, apply_status=?, apply_error=?, started_at=?, finished_at=NULL WHERE id=?", ("preflight", "", "", 0, "", "", now(), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "mission execution requested; running read-only preflight")
        try:
            run_preflight(plan, has_write, phase="execution")
        except PreflightWaitingForUser as exc:
            execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", str(exc), plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "execution paused · waiting for an explicit preflight choice")
            write_mission_docs(plan_id)
            return
        except PreflightBlocked as exc:
            execute("UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?", ("blocked", str(exc), now(), plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "execution stopped by a safety preflight blocker")
            write_mission_docs(plan_id)
            return
        if plan_is_paused(plan_id):
            log(orchestrator_log_id(plan_id), "supervisor", "mission pause arrived during preflight; no worker wave was started")
            write_mission_docs(plan_id)
            return
        execute("UPDATE plans SET status=? WHERE id=?", ("running", plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "mission execution started")
        write_mission_docs(plan_id)
        if has_write:
            checkpoint_exists = bool(
                plan.get("base_commit")
                and plan.get("integration_workspace")
                and Path(str(plan.get("integration_workspace"))).is_dir()
                and any(
                    task.get("status") in ("done", "executed", "waiting_for_orchestrator", "waiting_for_user")
                    for task in tasks
                )
            )
            if preserve_restart_checkpoint or checkpoint_exists:
                # Do not recreate an integration worktree after a restart: it
                # is the durable checkpoint containing completed write tasks or
                # a paused consultation. The same rule also protects a user
                # answer that resumes a partially integrated mission.
                ctx = stored_integration_context(plan)
                log(orchestrator_log_id(plan_id), "supervisor", "resuming from the persisted integration checkpoint; completed tasks will not rerun")
            else:
                ctx = prepare_integration(plan)
        else:
            info = repo_info(plan["workspace"])
            workspace = Path(plan["workspace"]).expanduser().resolve()
            ctx = {
                "repo_root": Path(info["root"]),
                "base_commit": info["head"],
                "workspace_rel": Path(info["rel"]),
                "integration_dir": Path(info["root"]),
                "integration_workspace": workspace,
                "integration_branch": "",
            }

        plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
        pending = {t["seq"]: t for t in tasks if t.get("status") not in ("done", "executed")}
        max_parallel = max(1, min(int(plan.get("max_parallel") or 4), MAX_PARALLEL_HARD))

        while pending:
            if plan_is_paused(plan_id):
                log(orchestrator_log_id(plan_id), "supervisor", "mission paused before the next worker wave")
                write_mission_docs(plan_id)
                return
            ready = ready_tasks(plan_id, pending)
            if not ready:
                # Resolve queued worker consultations only after all currently
                # runnable independent work has had a chance to execute. This
                # keeps one worker's question from pausing unrelated workers.
                queued_resolution = resolve_waiting_consultations(plan, list(pending.values()), ctx=ctx)
                if queued_resolution.get("paused") or plan_is_paused(plan_id):
                    log(orchestrator_log_id(plan_id), "supervisor", "mission pause preserved queued worker consultations")
                    write_mission_docs(plan_id)
                    return
                if queued_resolution.get("resolved"):
                    tasks = [one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task for task in tasks]
                    pending = {task["seq"]: task for task in tasks if task.get("status") not in ("done", "executed")}
                    continue
                if queued_resolution.get("waiting_for_user") or queued_resolution.get("attention") or queued_resolution.get("blocked"):
                    write_mission_docs(plan_id)
                    return
                waiting = [
                    task for task in pending.values()
                    if task.get("status") in ("waiting_for_orchestrator", "waiting_for_user", "paused_by_user")
                ]
                if waiting:
                    if any(task.get("status") == "paused_by_user" for task in waiting):
                        summary = "A worker is paused by you. Resume that worker to continue the dependent work."
                        execute("UPDATE plans SET status=?,summary=?,error=?,finished_at=NULL WHERE id=?", ("attention", summary, "A worker is paused by the user.", plan_id))
                        log(orchestrator_log_id(plan_id), "supervisor", summary)
                    log(orchestrator_log_id(plan_id), "supervisor", "no runnable tasks; waiting consultations remain durable")
                    break
                if pending:
                    for task in pending.values():
                        execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("blocked", "Dependency cycle or missing dependency", now(), task["id"]))
                break

            wave = ready[:max_parallel]
            log(orchestrator_log_id(plan_id), "supervisor", "launching wave: " + ", ".join(f"TASK-{t['seq']+1:03d} {t['title']}" for t in wave))
            if has_write:
                wave_base_commit = git(ctx["integration_dir"], "rev-parse", "HEAD").stdout.strip()
            else:
                wave_base_commit = ctx["base_commit"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
                futs = [pool.submit(run_parallel_task, plan, task, ctx, wave_base_commit) for task in wave]
                results = [f.result() for f in futs]

            if plan_is_paused(plan_id):
                # Pause is durable and wins any race with a worker finishing.
                # Preserve its worktree/thread checkpoint; never integrate a
                # result after the user has paused the mission.
                for result in results:
                    paused_task = result["task"]
                    current_task = one("SELECT status FROM tasks WHERE id=?", (paused_task["id"],)) or {}
                    if current_task.get("status") not in ("done", "executed"):
                        execute(
                            "UPDATE tasks SET status=?,error=?,finished_at=NULL,commit_hash=CASE WHEN ?<>'' THEN ? ELSE commit_hash END WHERE id=?",
                            ("paused_by_user", "Paused by user", result.get("commit") or "", result.get("commit") or "", paused_task["id"]),
                        )
                log(orchestrator_log_id(plan_id), "supervisor", "mission pause applied; worker checkpoints and thread ids were preserved")
                write_mission_docs(plan_id)
                return

            # Integrate only after the whole wave has completed, so workers truly run from the same snapshot.
            pause_for_user = False
            for result in sorted(results, key=lambda r: r["task"]["seq"]):
                task = result["task"]
                if result.get("cancelled"):
                    log(orchestrator_log_id(plan_id), "supervisor", f"TASK-{task['seq']+1:03d} stopped by user; mission will require attention")
                    pending.pop(task["seq"], None)
                    continue
                if result.get("paused"):
                    execute("UPDATE tasks SET status=?,error=?,finished_at=NULL WHERE id=?", ("paused_by_user", "Paused by user", task["id"]))
                    pending[task["seq"]] = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
                    log(orchestrator_log_id(plan_id), "supervisor", f"TASK-{task['seq']+1:03d} paused by user; dependent tasks remain waiting")
                    continue
                if pause_for_user and result.get("waiting_for_orchestrator"):
                    # Keep the durable consultation queued. It will be sent
                    # to the same orchestrator thread after the first user
                    # question is answered.
                    continue
                if result.get("waiting_for_orchestrator"):
                    resolution = resolve_worker_consultation(plan, task, result=result, ctx=ctx)
                    if resolution.get("retry"):
                        pending[task["seq"]] = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                    elif resolution.get("paused"):
                        log(orchestrator_log_id(plan_id), "supervisor", "mission pause preserved the active worker consultation")
                        write_mission_docs(plan_id)
                        return
                    elif resolution.get("waiting_for_user"):
                        pause_for_user = True
                    elif resolution.get("attention"):
                        pause_for_user = True
                    else:
                        pending.pop(task["seq"], None)
                    continue
                if result.get("blocked"):
                    resolution = resolve_worker_consultation(plan, task, result=result, ctx=ctx)
                    if resolution.get("retry"):
                        pending[task["seq"]] = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                    elif resolution.get("paused"):
                        log(orchestrator_log_id(plan_id), "supervisor", "mission pause preserved the active worker consultation")
                        write_mission_docs(plan_id)
                        return
                    elif resolution.get("waiting_for_user"):
                        pause_for_user = True
                    elif resolution.get("attention"):
                        pause_for_user = True
                    else:
                        pending.pop(task["seq"], None)
                    continue
                if not result.get("ok") and not result.get("blocked"):
                    retry = resolve_worker_failure(plan, task, result, ctx)
                    if retry:
                        pending[task["seq"]] = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                        continue
                if result["write"]:
                    integrate_write_result(plan, ctx, result)
                else:
                    mark_read_result_done(result)
                pending.pop(task["seq"], None)
            log(orchestrator_log_id(plan_id), "supervisor", "wave complete; integrated successful results and resolved escalations")
            write_mission_docs(plan_id)
            if pause_for_user:
                log(orchestrator_log_id(plan_id), "supervisor", "mission paused until the user answers a worker consultation")
                # Independent tasks from the same or a later wave may still
                # run. waiting_for_user tasks are filtered out by ready_tasks;
                # their dependents remain blocked until the answer arrives.
                write_mission_docs(plan_id)

        task_rows = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
        all_done = bool(task_rows) and all(t["status"] in ("done", "executed") for t in task_rows)

        waiting_rows = [
            task for task in task_rows
            if task.get("status") in ("waiting_for_orchestrator", "waiting_for_user")
        ]
        if waiting_rows:
            has_user_question = any(task.get("status") == "waiting_for_user" for task in waiting_rows)
            summary = (
                "Independent work completed. One or more workers are waiting for your information."
                if has_user_question
                else "Independent work completed. Worker consultations are queued for the orchestrator."
            )
            execute(
                "UPDATE plans SET status=?,summary=?,finished_at=NULL WHERE id=?",
                ("waiting_for_user" if has_user_question else "running", summary, plan_id),
            )
            log(orchestrator_log_id(plan_id), "supervisor", summary)
            write_mission_docs(plan_id)
            return

        if read_only_only and not all_done:
            selected_read_seqs = {t["seq"] for t in tasks}
            read_done = all(
                t["status"] in ("done", "executed")
                for t in task_rows
                if t["seq"] in selected_read_seqs
            )
            write_pending = any(t["mode"] == "write" and t["status"] not in ("done", "executed") for t in task_rows)
            if read_done and write_pending:
                summary = "Read-only checks completed. Write tasks remain paused until the workspace choice is resolved."
                execute("UPDATE plans SET status=?,summary=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", summary, "Write tasks are waiting for an explicit preflight choice.", plan_id))
                log(orchestrator_log_id(plan_id), "supervisor", "read-only continuation complete; write tasks remain waiting for user")
                write_mission_docs(plan_id)
                return
            summary = "Read-only phase complete. Dependent or write tasks remain paused."
            execute("UPDATE plans SET status=?,summary=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", summary, "The selected read-only graph is complete; remaining tasks require the next execution phase.", plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "read-only phase checkpoint saved; final synthesis deferred")
            write_mission_docs(plan_id)
            return

        paused_rows = [task for task in task_rows if task.get("status") == "paused_by_user"]
        if paused_rows:
            summary = "A worker is paused by you. Resume it to continue the mission."
            execute("UPDATE plans SET status=?,summary=?,error=?,finished_at=NULL WHERE id=?", ("attention", summary, "A worker is paused by the user.", plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", summary)
            write_mission_docs(plan_id)
            return

        if not all_done:
            summary = "Mission paused because one or more tasks did not complete; final synthesis was deferred."
            current_error = (one("SELECT error FROM plans WHERE id=?", (plan_id,)) or {}).get("error") or ""
            execute(
                "UPDATE plans SET status=?,summary=?,error=?,finished_at=NULL WHERE id=?",
                ("attention", summary, current_error, plan_id),
            )
            log(orchestrator_log_id(plan_id), "supervisor", "mission incomplete; final synthesis deferred until every required task completes")
            write_mission_docs(plan_id)
            return

        synth_workspace = ctx["integration_workspace"] if has_write else Path(plan["workspace"])
        try:
            log(orchestrator_log_id(plan_id), "supervisor", "starting final orchestrator synthesis")
            turn = run_mission_orchestrator_turn(
                plan_id,
                "final_synthesis",
                synthesis_prompt(plan, task_rows),
                mode="read",
                transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
            )
            summary, used_model = turn["text"], turn["model"]
            execute("UPDATE plans SET summary=?, orchestrator_used=? WHERE id=?", (summary, used_model, plan_id))
        except Exception as e:
            summary = f"Final orchestrator synthesis failed: {e}"
            execute("UPDATE plans SET summary=? WHERE id=?", (summary, plan_id))

        if all_done and has_write:
            # Re-check the user's working tree before presenting the integrated patch.
            try:
                run_preflight(plan, True, phase="apply")
            except PreflightWaitingForUser as exc:
                execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("waiting_for_user", str(exc), plan_id))
                log(orchestrator_log_id(plan_id), "supervisor", "integration ready but waiting for an explicit workspace choice before apply")
                write_mission_docs(plan_id)
                return
            except PreflightBlocked as exc:
                execute("UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?", ("blocked", str(exc), now(), plan_id))
                log(orchestrator_log_id(plan_id), "supervisor", "apply stopped by a safety preflight blocker")
                write_mission_docs(plan_id)
                return
            if not integration_patch(ctx).strip():
                execute("UPDATE plans SET status=?, applied=0, apply_status=?, apply_error=?, finished_at=? WHERE id=?", ("done", "no_changes", "", now(), plan_id))
                cleanup_successful_plan(ctx, plan_id)
                log(orchestrator_log_id(plan_id), "supervisor", "mission completed without an integration diff")
            else:
                end_usage = quota_status(force=True, wait=True)
                execute("UPDATE plans SET status=?, apply_status=?, apply_error=?, usage_end_json=?, finished_at=? WHERE id=?", ("awaiting_apply", "ready", "", json.dumps(end_usage), now(), plan_id))
                log(orchestrator_log_id(plan_id), "supervisor", "integration diff ready; waiting for user Apply changes approval")
                write_mission_docs(plan_id)
                return

        end_usage = quota_status(force=True, wait=True)
        execute("UPDATE plans SET status=?, usage_end_json=?, finished_at=? WHERE id=?", ("done" if all_done else "attention", json.dumps(end_usage), now(), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "mission finished: " + ("done" if all_done else "attention"))
        write_mission_docs(plan_id)
    except Exception as e:
        current_plan = one("SELECT * FROM plans WHERE id=?", (plan_id,)) or {}
        if current_plan.get("status") in ("paused", "pausing") or int(current_plan.get("paused") or 0):
            execute("UPDATE plans SET status=?,error=?,apply_error=?,finished_at=NULL WHERE id=?", ("paused", "", "", plan_id))
            log(orchestrator_log_id(plan_id), "supervisor", "mission paused; the current turn ended without resetting completed tasks")
            write_mission_docs(plan_id)
            return
        end_usage = quota_status(force=True, wait=True)
        execute("UPDATE plans SET status=?, error=?, apply_status=?, apply_error=?, usage_end_json=?, finished_at=? WHERE id=?", ("attention", str(e), "failed" if plan.get("status") == "awaiting_apply" else "", str(e), json.dumps(end_usage), now(), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", f"mission attention: {e}")
        write_mission_docs(plan_id)
    finally:
        release_plan_run(plan_id)


def latest_orchestrator_session(plan_id):
    return one(
        """SELECT * FROM agent_sessions
           WHERE plan_id=? AND kind LIKE '%orchestrator%'
           ORDER BY CASE WHEN thread_id!='' THEN 0 ELSE 1 END, started_at DESC, rowid DESC
           LIMIT 1""",
        (plan_id,),
    )


def orchestrator_session_usage(session_id):
    """Extract the provider-reported token usage for the latest orchestrator turn."""
    if not session_id:
        return {}
    events = rows(
        "SELECT event_type,payload_json FROM agent_events WHERE session_id=? ORDER BY id DESC LIMIT 120",
        (session_id,),
    )
    for event in events:
        if event.get("event_type") not in ("turn.completed", "turn/completed", "response.completed"):
            continue
        payload = safe_json(event.get("payload_json"), {})
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        usage = payload.get("usage") or params.get("usage") or turn.get("usage") or {}
        if isinstance(usage, dict):
            return usage
    return {}


def plan_consultations(plan_id):
    result = rows(
        "SELECT * FROM consultations WHERE plan_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
        (plan_id,),
    )
    for item in result:
        item["evidence"] = safe_json(item.get("evidence_json"), [])
        item["options"] = safe_json(item.get("options_json"), [])
        item["orchestrator_response"] = safe_json(item.get("orchestrator_response_json"), {})
        item["user_questions"] = safe_json(item.get("user_questions_json"), [])
        item["user_answer"] = safe_json(item.get("user_answer_json"), {})
    return result


def queued_messages(task_id):
    return rows("SELECT * FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id", (task_id,))


def consume_queued_messages(plan, task, workspace, model, effort, tier):
    """Deliver user messages queued while the current Codex turn was working.

    CLI mode cannot steer an in-flight `codex exec` turn, so messages are held for the
    next turn on the same Codex thread. This is explicit in the UI and keeps the
    conversation continuous without racing the worker process.
    """
    delivered=[]
    while True:
        msg=one("SELECT * FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id LIMIT 1", (task['id'],))
        if not msg:
            break
        execute("UPDATE task_messages SET status=? WHERE id=?", ("sending", msg['id']))
        latest=latest_agent_session(task['id'])
        thread_id=(latest or {}).get('thread_id') or ''
        images=safe_json(msg.get('attachments_json'), [])
        log(task['id'], 'manual', 'queued user message is being delivered on the next Codex turn')
        try:
            out=run_codex(msg.get('text') or 'Continue the assigned task using the attached context.', workspace, task.get('mode') or 'read', model, task['id'], effort, tier, images=images, resume_thread_id=thread_id, session_kind='manual')
            execute("UPDATE task_messages SET status=?, error=? WHERE id=?", ("delivered", "", msg['id']))
            delivered.append(out)
        except Exception as e:
            execute("UPDATE task_messages SET status=?, error=? WHERE id=?", ("failed", str(e), msg['id']))
            log(task['id'], 'manual', f'queued message failed: {e}')
            break
    return delivered


def run_orchestrator_followup(plan_id, prompt, image_paths=None):
    plan=one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError('Mission bulunamadı')
    if int(plan.get('demo_mode') or 0):
        oid=orchestrator_log_id(plan_id)
        log(oid, 'manual', f'demo user → orchestrator: {prompt}')
        time.sleep(0.35)
        log(oid, 'supervisor', 'demo orchestrator received the manual instruction; task assignments remain user-controlled in Plan Review')
        return
    log(orchestrator_log_id(plan_id), 'manual', f'user → orchestrator: {prompt}')
    turn = run_mission_orchestrator_turn(
        plan_id,
        "manual_message",
        prompt,
        mode="read",
        images=image_paths or [],
        # This is a new orchestrator turn, so use the current mission setting.
        # The model used by the previous turn must not pin future turns after
        # the user changes Runtime settings.
        requested_model=plan.get("orchestrator_model"),
        transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
    )
    log(
        orchestrator_log_id(plan_id),
        "manual",
        "orchestrator answered the conversation message on the same mission thread",
    )


def answer_consultation(plan_id, consultation_id, answer, attachments=None):
    """Persist a user answer, resolve it on the same root thread, then resume work."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    consultation = one("SELECT * FROM consultations WHERE id=? AND plan_id=?", (consultation_id, plan_id))
    if not plan or not consultation:
        raise ValueError("Consultation bulunamadı")
    if consultation.get("status") != "waiting_for_user":
        raise ValueError("Bu consultation artık kullanıcı cevabı beklemiyor")
    answer_text = str(answer or "").strip()
    attachment_paths = [str(x) for x in (attachments or []) if x and Path(str(x)).is_file()]
    if not answer_text and not attachment_paths:
        raise ValueError("Bir cevap veya dosya eklenmelidir")
    answer_payload = {
        "text": answer_text,
        "attachments": attachment_paths,
        "answered_at": now(),
    }
    execute(
        "UPDATE consultations SET status=?,user_answer_json=? WHERE id=?",
        ("resolving", json.dumps(answer_payload, ensure_ascii=False), consultation_id),
    )
    record_control_event(plan_id, "agentdock.user_answer", {
        "consultation_id": consultation_id,
        "task_id": consultation.get("task_id") or "",
        "answer": answer_payload,
    }, task_id=consultation.get("task_id") or "")
    log(orchestrator_log_id(plan_id), "supervisor", f"user answer recorded for consultation {consultation_id[:12]}")
    task = one("SELECT * FROM tasks WHERE id=?", (consultation["task_id"],))
    if not task:
        raise ValueError("Consultation task bulunamadı")
    resolved = resolve_worker_consultation(
        plan,
        task,
        result={"consultation_id": consultation_id},
        user_answer=answer_payload,
    )
    if resolved.get("retry"):
        execute(
            "UPDATE plans SET status=?,pending_question_id=?,pending_question_json=?,error=?,finished_at=NULL WHERE id=?",
            ("approved", "", "{}", "", plan_id),
        )
        log(orchestrator_log_id(plan_id), "supervisor", "user answer accepted · resuming the same worker and remaining graph")
        if claim_plan_run(plan_id):
            threading.Thread(
                target=run_plan,
                args=(plan_id,),
                kwargs={"claimed": True},
                daemon=True,
            ).start()
        return {"ok": True, "status": "approved", "resuming": True}
    if resolved.get("waiting_for_user"):
        return {"ok": True, "status": "waiting_for_user", "pending": resolved.get("pending") or {}}
    if resolved.get("blocked"):
        return {"ok": False, "status": "blocked", "error": resolved.get("error") or ""}
    return {"ok": False, "status": "attention", "error": resolved.get("error") or "Consultation could not be resolved."}


def reconstruct_orchestrator_context(plan_id):
    """Start a new orchestrator generation only after an explicit user action."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    if plan.get("status") in ("planning", "preflight", "running"):
        raise ValueError("Mission zaten çalışıyor")
    task_rows = rows("SELECT seq,title,status,output,error,contract_json FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
    context = f"""The user explicitly requested: Reconstruct orchestrator context.

MISSION:
{plan['goal']}

Persisted mission state:
{json.dumps({
    "status": plan.get("status"),
    "decision": plan.get("decision"),
    "summary": plan.get("summary"),
    "tasks": task_rows,
    "legacy_state": plan.get("legacy_orchestrator_status"),
}, ensure_ascii=False, indent=2)[:80000]}

Reconstruct a single coherent control-plane context from the persisted mission
records. Do not execute workers in this turn and do not silently discard any
completed task output. Briefly acknowledge the reconstructed context and state
what the next safe action is.
"""
    execute(
        "UPDATE plans SET orchestrator_turn_status=?,orchestrator_last_error=?,legacy_orchestrator_status=? WHERE id=?",
        ("reconstructing", "", "reconstructing", plan_id),
    )
    log(orchestrator_log_id(plan_id), "supervisor", "explicit orchestrator context reconstruction requested by user")
    try:
        turn = run_mission_orchestrator_turn(
            plan_id,
            "reconstruct",
            context,
            mode="read",
            transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
        )
        execute(
            "UPDATE plans SET legacy_orchestrator_status=?,orchestrator_last_error=?,summary=? WHERE id=?",
            ("reconstructed", "", turn.get("text") or "", plan_id),
        )
        log(orchestrator_log_id(plan_id), "supervisor", "orchestrator context reconstructed; future turns use the new thread")
        write_mission_docs(plan_id)
        return {"ok": True, "thread_id": turn.get("thread_id") or "", "generation": one("SELECT orchestrator_generation FROM plans WHERE id=?", (plan_id,)).get("orchestrator_generation")}
    except Exception:
        execute(
            "UPDATE plans SET legacy_orchestrator_status=?,orchestrator_turn_status=? WHERE id=?",
            ("reconstruct_required", "attention", plan_id),
        )
        write_mission_docs(plan_id)
        raise


def _run_reconstruct_and_release(plan_id):
    try:
        reconstruct_orchestrator_context(plan_id)
    except Exception as exc:
        log(orchestrator_log_id(plan_id), "supervisor", f"orchestrator reconstruction failed: {exc}")
    finally:
        release_plan_run(plan_id)


def open_terminal_at(path):
    path=str(Path(path).expanduser().resolve())
    if not Path(path).is_dir():
        raise ValueError('Terminal klasörü artık mevcut değil')
    if sys.platform == 'darwin':
        script=f'tell application "Terminal" to do script "cd {shlex.quote(path)}"'
        subprocess.Popen(['osascript','-e',script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise ValueError('Open Terminal currently supports macOS only')


def run_single_task(task_id):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    workspace = task.get("workspace") or plan["workspace"]
    result = run_task_once(plan, task, workspace, force_mode=task["mode"])
    if result["ok"]:
        execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))


def run_manual_followup_message(message_id):
    msg=one("SELECT * FROM task_messages WHERE id=?",(message_id,))
    if not msg: return
    execute("UPDATE task_messages SET status=? WHERE id=?",('sending',message_id))
    try:
        run_manual_followup(msg['task_id'], msg.get('text') or 'Continue.', safe_json(msg.get('attachments_json'), []))
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?",('delivered','',message_id))
    except Exception as e:
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?",('failed',str(e),message_id))


def run_manual_followup(task_id, prompt, image_paths=None):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    if plan and int(plan.get("demo_mode") or 0):
        return run_demo_manual_followup(task_id, prompt, image_paths)
    latest = latest_agent_session(task_id)
    thread_id = str(task.get("worker_thread_id") or (latest or {}).get("thread_id") or "")
    if not thread_id:
        raise ValueError("Bu agent için devam ettirilecek Codex conversation bulunamadı.")
    workspace = task.get("workspace") or ""
    mode = task.get("mode") or "read"
    if not workspace or not Path(workspace).is_dir():
        # Successful isolated worktrees are cleaned up after integration. A
        # completed worker must still be conversationally resumable, so use
        # the final workspace for a read-only follow-up instead of opening a
        # new worker session or pretending the old worktree still exists.
        workspace = str((plan or {}).get("integration_workspace") or "")
        if not workspace or not Path(workspace).is_dir():
            workspace = str((plan or {}).get("workspace") or "")
        if not workspace or not Path(workspace).is_dir():
            raise ValueError("Bu task için devam ettirilecek workspace artık mevcut değil.")
        if mode == "write":
            mode = "read"
            log(task_id, "manual", "completed worker conversation continued in the final workspace read-only")
    agent = one("SELECT * FROM agents WHERE id=?", (task.get("agent_id"),)) or {}
    model = task_model(plan, agent, task)
    effort = task_effort(plan, agent, task)
    tier = task_tier(plan, agent, task)
    log(task_id, "manual", "conversation message started on the same worker thread")
    try:
        out = run_codex(prompt, workspace, mode, model, task_id, effort, tier,
                        images=image_paths or [], resume_thread_id=thread_id, session_kind="manual")
        log(task_id, "manual", "conversation response completed")
        write_mission_docs(plan["id"])
        return out
    except Exception as e:
        log(task_id, "manual", f"conversation response failed: {e}")
        write_mission_docs(plan["id"])
        raise


def pause_task(task_id, reason="Paused by user"):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    if task.get("status") in ("done", "executed", "cancelled"):
        raise ValueError("Bu task artık duraklatılamaz")
    thread_id = str(task.get("worker_thread_id") or (latest_agent_session(task_id) or {}).get("thread_id") or "").strip()
    execute(
        "UPDATE tasks SET status=?,error=?,pause_reason=?,worker_thread_id=?,finished_at=NULL WHERE id=?",
        ("paused_by_user", reason, "user", thread_id, task_id),
    )
    interrupt_app_server(task_id)
    with RUNNERS_LOCK:
        proc = RUNNERS.get(task_id)
    if proc:
        terminate_process(proc)
    log(task_id, "manual", "worker paused by user; same conversation will be resumed on request")
    log(orchestrator_log_id(task["plan_id"]), "supervisor", f"TASK-{task['seq']+1:03d} paused by user")
    write_mission_docs(task["plan_id"])
    return {"ok": True, "status": "paused_by_user", "thread_id": thread_id}


def resume_task(task_id):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    if task.get("status") not in ("paused_by_user", "failed", "attention"):
        raise ValueError("Bu task devam ettirilebilir durumda değil")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    if plan and plan.get("pending_question_id"):
        raise ValueError("Mission önce bekleyen kullanıcı sorusunun cevabını bekliyor")
    thread_id = str(task.get("worker_thread_id") or (latest_agent_session(task_id) or {}).get("thread_id") or "").strip()
    if not thread_id:
        message = "Worker conversation is unavailable; explicit task recovery is required."
        execute("UPDATE tasks SET status=?,error=?,pause_reason=? WHERE id=?", ("attention", message, "resume_requires_thread", task_id))
        log(task_id, "supervisor", message)
        write_mission_docs(task["plan_id"])
        return {"ok": False, "status": "attention", "error": message}
    execute("UPDATE tasks SET worker_thread_id=? WHERE id=?", (thread_id, task_id))
    resume_message = same_worker_resume_handoff(task)
    execute(
        "UPDATE tasks SET status=?,error=?,pause_reason='',worker_resume_message=?,finished_at=NULL WHERE id=?",
        ("resuming", "", resume_message, task_id),
    )
    log(task_id, "manual", "worker resume requested; continuing the same conversation")
    if plan:
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} resume requested on the same worker thread")
        write_mission_docs(plan["id"])
        if plan.get("status") in ("attention", "waiting_for_user"):
            execute("UPDATE plans SET status=?,error=?,finished_at=NULL WHERE id=?", ("approved", "", plan["id"]))
        if plan.get("status") not in ("running", "paused", "pausing") and claim_plan_run(plan["id"]):
            threading.Thread(target=run_plan, args=(plan["id"],), kwargs={"claimed": True}, daemon=True).start()
    return {"ok": True, "status": "resuming", "thread_id": thread_id}


def pause_plan(plan_id, reason="Paused by user"):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    if plan.get("status") in ("done", "cancelled", "blocked", "paused", "pausing"):
        raise ValueError("Bu mission duraklatılamaz")
    if plan.get("pending_question_id"):
        raise ValueError("Mission önce bekleyen kullanıcı sorusunun cevabını bekliyor")
    execute(
        "UPDATE plans SET status=?,paused=1,pause_reason=?,pause_requested_at=?,error='',finished_at=NULL WHERE id=?",
        ("pausing", reason, now(), plan_id),
    )
    running_tasks = rows(
        "SELECT * FROM tasks WHERE plan_id=? AND status IN ('running','resuming')",
        (plan_id,),
    )
    for task in running_tasks:
        thread_id = str(task.get("worker_thread_id") or (latest_agent_session(task["id"]) or {}).get("thread_id") or "").strip()
        execute(
            "UPDATE tasks SET status=?,error=?,pause_reason=?,worker_thread_id=?,worker_resume_message=?,finished_at=NULL WHERE id=?",
            ("paused_by_user", reason, "mission", thread_id, same_worker_resume_handoff(task, "The mission was paused by the user. Resume this same task when the mission continues."), task["id"]),
        )
        interrupt_app_server(task["id"])
        with RUNNERS_LOCK:
            proc = RUNNERS.get(task["id"])
        if proc:
            terminate_process(proc)
    orchestrator_id = orchestrator_log_id(plan_id)
    interrupt_app_server(orchestrator_id)
    with RUNNERS_LOCK:
        orchestrator_proc = RUNNERS.get(orchestrator_id)
    if orchestrator_proc:
        terminate_process(orchestrator_proc)
    execute("UPDATE plans SET status=? WHERE id=?", ("paused", plan_id))
    log(orchestrator_id, "manual", "mission paused by user; active turns were interrupted and checkpoints preserved")
    write_mission_docs(plan_id)
    return {"ok": True, "status": "paused", "paused_tasks": len(running_tasks)}


def resume_plan(plan_id):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    if plan.get("status") not in ("paused", "attention", "waiting_for_user"):
        raise ValueError("Bu mission şu anda devam ettirilebilir durumda değil")
    if plan.get("pending_question_id"):
        raise ValueError("Mission önce bekleyen kullanıcı sorusunun cevabını bekliyor")
    if not str(plan.get("decision") or "").strip():
        # A pause can arrive during the initial disposition turn. Resume that
        # turn as planning; do not send an empty plan into execution.
        execute(
            "UPDATE plans SET status='planning',paused=0,pause_reason='',error='',pause_requested_at=NULL,finished_at=NULL WHERE id=?",
            (plan_id,),
        )
        log(orchestrator_log_id(plan_id), "manual", "mission planning resumed; continuing the same orchestrator conversation")
        write_mission_docs(plan_id)
        if claim_plan_run(plan_id):
            threading.Thread(target=build_plan, args=(plan_id,), daemon=True).start()
            return {"ok": True, "status": "resuming"}
        return {"ok": False, "status": "planning", "message": "Mission is already planning."}
    if not one("SELECT id FROM tasks WHERE plan_id=? LIMIT 1", (plan_id,)):
        # A no-task disposition can be waiting for a user, blocked, or an
        # attention state from a prior attempt. Resume it by asking the same
        # orchestrator conversation for a fresh disposition; never send an
        # empty graph through execution.
        execute(
            """UPDATE plans SET status='planning',paused=0,pause_reason='',error='',
               decision='',decision_reason='',evidence_json='[]',questions_json='[]',
               final_response='',summary='',pending_question_id='',pending_question_json='{}',
               pause_requested_at=NULL,finished_at=NULL WHERE id=?""",
            (plan_id,),
        )
        log(orchestrator_log_id(plan_id), "manual", "mission disposition reopened; re-evaluating without creating an empty task graph")
        write_mission_docs(plan_id)
        if claim_plan_run(plan_id):
            threading.Thread(target=build_plan, args=(plan_id,), daemon=True).start()
            return {"ok": True, "status": "resuming"}
        return {"ok": False, "status": "planning", "message": "Mission is already planning."}
    mission_paused = rows(
        "SELECT * FROM tasks WHERE plan_id=? AND status='paused_by_user' AND pause_reason='mission'",
        (plan_id,),
    )
    missing_threads = [
        task for task in mission_paused
        if not str(task.get("worker_thread_id") or (latest_agent_session(task["id"]) or {}).get("thread_id") or "").strip()
    ]
    if missing_threads:
        message = "A paused worker has no resumable conversation; explicit task recovery is required."
        for task in missing_threads:
            execute(
                "UPDATE tasks SET status=?,error=?,pause_reason=? WHERE id=?",
                ("attention", message, "resume_requires_thread", task["id"]),
            )
        execute(
            "UPDATE plans SET status=?,paused=0,pause_reason='',error=?,finished_at=NULL WHERE id=?",
            ("attention", message, plan_id),
        )
        log(orchestrator_log_id(plan_id), "supervisor", message)
        write_mission_docs(plan_id)
        return {"ok": False, "status": "attention", "error": message}
    for task in mission_paused:
        thread_id = str(task.get("worker_thread_id") or (latest_agent_session(task["id"]) or {}).get("thread_id") or "").strip()
        execute(
            "UPDATE tasks SET worker_thread_id=?,status='pending',error='',pause_reason='',finished_at=NULL WHERE id=?",
            (thread_id, task["id"]),
        )
    # Only tasks paused as part of the mission-wide action are resumed here;
    # a worker paused individually remains paused until its own Resume action.
    execute(
        "UPDATE tasks SET status='pending',error='',finished_at=NULL WHERE plan_id=? AND status='resuming'",
        (plan_id,),
    )
    execute(
        "UPDATE plans SET status=?,paused=0,pause_reason='',error='',pause_requested_at=NULL,resume_count=resume_count+1,finished_at=NULL WHERE id=?",
        ("approved", plan_id),
    )
    log(orchestrator_log_id(plan_id), "manual", "mission resume requested; preserving completed tasks and conversation threads")
    write_mission_docs(plan_id)
    if claim_plan_run(plan_id):
        threading.Thread(target=run_plan, args=(plan_id,), kwargs={"claimed": True}, daemon=True).start()
        return {"ok": True, "status": "resuming"}
    return {"ok": False, "status": "running", "message": "Mission is already running."}


def mission_config(plan_id, data):
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    values = {
        "orchestrator_model": (data.get("orchestrator_model") or plan.get("orchestrator_model") or DEFAULT_ORCHESTRATOR).strip(),
        "orchestrator_effort": (data.get("orchestrator_effort") or plan.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT).strip(),
        "orchestrator_tier": (data.get("orchestrator_tier") or plan.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER).strip(),
        "worker_model": (data.get("worker_model") or plan.get("worker_model") or DEFAULT_WORKER).strip(),
        "worker_effort": (data.get("worker_effort") or plan.get("worker_effort") or DEFAULT_WORKER_EFFORT).strip(),
        "worker_tier": (data.get("worker_tier") or plan.get("worker_tier") or DEFAULT_WORKER_TIER).strip(),
    }
    validate_runtime_config(values["orchestrator_model"], values["orchestrator_effort"], values["orchestrator_tier"], "Orchestrator")
    validate_runtime_config(values["worker_model"], values["worker_effort"], values["worker_tier"], "Worker default")
    max_parallel = max(1, min(int(data.get("max_parallel") or plan.get("max_parallel") or 4), MAX_PARALLEL_HARD))
    apply_remaining = bool(data.get("apply_remaining"))
    execute(
        """UPDATE plans SET orchestrator_model=?,orchestrator_effort=?,orchestrator_tier=?,
           worker_model=?,worker_effort=?,worker_tier=?,max_parallel=? WHERE id=?""",
        (values["orchestrator_model"], values["orchestrator_effort"], values["orchestrator_tier"],
         values["worker_model"], values["worker_effort"], values["worker_tier"], max_parallel, plan_id),
    )
    if apply_remaining:
        execute(
            """UPDATE tasks SET model_override=?,reasoning_effort_override=?,service_tier_override=?
               WHERE plan_id=? AND status IN ('pending','paused_by_user','failed','attention','waiting_for_orchestrator','waiting_for_user','blocked','resuming')""",
            (values["worker_model"], values["worker_effort"], values["worker_tier"], plan_id),
        )
    record_control_event(plan_id, "agentdock.mission_config", {"config": values, "max_parallel": max_parallel, "apply_remaining": apply_remaining})
    log(orchestrator_log_id(plan_id), "manual", "mission runtime settings updated" + (" for remaining work" if apply_remaining else " for future turns"))
    write_mission_docs(plan_id)
    return {"ok": True, "config": {**values, "max_parallel": max_parallel}, "apply_remaining": apply_remaining}


def restart_as_new_mission(plan_id):
    source = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not source:
        raise ValueError("Mission bulunamadı")
    new_id = str(uuid.uuid4())[:8]
    execute(
        """INSERT INTO plans(
           id,goal,title,workspace,planner_engine,status,created_at,orchestrator_model,
           worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,
           worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,
           automation_mode,attachments_json,demo_mode
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id, source.get("goal") or "", deterministic_mission_title(source.get("goal")),
            source.get("workspace") or "", source.get("planner_engine") or "codex-chatgpt", "planning", now(),
            source.get("orchestrator_model") or DEFAULT_ORCHESTRATOR, source.get("worker_model") or DEFAULT_WORKER,
            int(source.get("max_parallel") or 4), str(mission_dir(new_id)), "{}",
            source.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT,
            source.get("worker_effort") or DEFAULT_WORKER_EFFORT,
            source.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER,
            source.get("worker_tier") or DEFAULT_WORKER_TIER,
            source.get("recovery_json") or json.dumps(RECOVERY_DEFAULTS), source.get("workspace_id") or "",
            source.get("automation_mode") or "auto", "[]", int(source.get("demo_mode") or 0),
        ),
    )
    write_mission_docs(new_id)
    worker = build_demo_plan if int(source.get("demo_mode") or 0) else build_plan
    threading.Thread(target=worker, args=(new_id,), daemon=True).start()
    return {"ok": True, "plan_id": new_id, "status": "planning"}


def task_diff(task):
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    if plan and int(plan.get("demo_mode") or 0):
        return demo_task_diff(task)
    workspace = task.get("workspace") or ""
    commit_hash = task.get("commit_hash") or ""
    repo = repo_info(plan["workspace"])
    if commit_hash and repo.get("is_git"):
        r = git(repo["root"], "show", "--format=", "--stat", "--patch", commit_hash, check=False)
        return (r.stdout or r.stderr)[-120000:]
    if workspace and Path(workspace).is_dir():
        r = git(workspace, "diff", "--stat", "--patch", check=False)
        return (r.stdout or r.stderr)[-120000:]
    return "Diff is no longer available for this task worktree."


def timeline_for(target_id):
    """Merge Codex events, control logs and user messages into one feed."""
    events = rows(
        "SELECT id,session_id,ts,event_type,item_type,payload_json FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 600",
        (target_id,),
    )
    logs = rows(
        "SELECT id,ts,stream,line FROM logs WHERE task_id=? AND stream IN ('stderr','manual','supervisor','system') ORDER BY id DESC LIMIT 600",
        (target_id,),
    )
    messages = []
    if not str(target_id).startswith("orchestrator:"):
        messages = rows(
            "SELECT id,ts,text,status,error,attachments_json FROM task_messages WHERE task_id=? ORDER BY ts DESC,id DESC LIMIT 200",
            (target_id,),
        )
    items = []
    for item in reversed(events):
        item["kind"] = "event"
        item["payload"] = safe_json(item.pop("payload_json", "{}"), {})
        item["order"] = int(item.get("id") or 0)
        items.append(item)
    for item in reversed(messages):
        item["kind"] = "message"
        item["order"] = int(item.get("id") or 0) if str(item.get("id") or "").isdigit() else 0
        item["attachments"] = safe_json(item.pop("attachments_json", "[]"), [])
        items.append(item)
    for item in reversed(logs):
        item["kind"] = "log"
        item["order"] = int(item.get("id") or 0)
        items.append(item)
    rank = {"event": 1, "message": 2, "log": 3}
    items.sort(key=lambda item: (int(item.get("ts") or 0), rank.get(item.get("kind"), 9), item.get("order", 0)))
    # The UI renders one terminal-like stream. Expose a stable display
    # sequence after merging the three persisted sources so callers never
    # need to guess which local table's id should win a timestamp tie.
    for sequence, item in enumerate(items, 1):
        item["sequence"] = sequence
    return {"session": latest_agent_session(target_id), "items": items}


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        rel = urlparse(path).path.lstrip("/") or "index.html"
        static_root = STATIC.resolve()
        candidate = (static_root / rel).resolve()
        if candidate != static_root and static_root not in candidate.parents:
            return str(static_root / "__agentdock_missing_file__")
        return str(candidate)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/health":
            engines = engine_status()
            return self.send_json({
                "ok": True,
                "version": "0.12.0",
                "db": str(DB),
                "engines": engines,
                "codex_transport": CODEX_TRANSPORT,
                "server_time": now(),
            })
        if p == "/api/state":
            plans = rows("SELECT * FROM plans ORDER BY created_at DESC LIMIT 100")
            for pl in plans:
                pl["tasks"] = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (pl["id"],))
                pl["repo"] = repo_info(pl["workspace"])
                pl["attachments"] = rows("SELECT id,name,mime,path,created_at FROM attachments WHERE plan_id=? ORDER BY created_at", (pl["id"],))
                pl["evidence"] = safe_json(pl.get("evidence_json"), [])
                pl["questions"] = safe_json(pl.get("questions_json"), [])
                pl["workspace_snapshot"] = safe_json(pl.get("workspace_snapshot_json"), {})
                pl["pending_question"] = safe_json(pl.get("pending_question_json"), {})
                pl["consultations"] = plan_consultations(pl["id"])
            workspaces = [workspace_summary(w) for w in rows("SELECT * FROM workspaces ORDER BY last_opened_at DESC, created_at DESC")]
            return self.send_json(
                {
                    "agents": rows("SELECT * FROM agents ORDER BY created_at"),
                    "plans": plans,
                    "workspaces": workspaces,
                    "engines": engine_status(),
                    "quota": quota_status(),
                    "defaults": {
                        "orchestrator": DEFAULT_ORCHESTRATOR,
                        "worker": DEFAULT_WORKER,
                        "orchestrator_effort": DEFAULT_ORCHESTRATOR_EFFORT,
                        "worker_effort": DEFAULT_WORKER_EFFORT,
                        "orchestrator_tier": DEFAULT_ORCHESTRATOR_TIER,
                        "worker_tier": DEFAULT_WORKER_TIER,
                        "max_parallel": 4,
                        "recovery": RECOVERY_DEFAULTS,
                    },
                }
            )
        if p.startswith("/api/live/"):
            pid = p.split("/")[-1]
            plan = one("SELECT * FROM plans WHERE id=?", (pid,))
            if not plan:
                return self.send_json({"error": "plan not found"}, 404)
            plan["attachments"] = rows("SELECT id,name,mime,path,created_at FROM attachments WHERE plan_id=? ORDER BY created_at", (pid,))
            plan["evidence"] = safe_json(plan.get("evidence_json"), [])
            plan["questions"] = safe_json(plan.get("questions_json"), [])
            plan["workspace_snapshot"] = safe_json(plan.get("workspace_snapshot_json"), {})
            plan["pending_question"] = safe_json(plan.get("pending_question_json"), {})
            consultations = plan_consultations(pid)
            tasks = rows(
                """SELECT t.*, a.name AS agent_name, a.role AS agent_role, a.model AS agent_model,
                          a.reasoning_effort AS agent_effort, a.service_tier AS agent_tier
                   FROM tasks t LEFT JOIN agents a ON a.id=t.agent_id
                   WHERE t.plan_id=? ORDER BY t.seq""",
                (pid,),
            )
            for task in tasks:
                latest = rows(
                    "SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 18",
                    (task["id"],),
                )
                task["recent_logs"] = list(reversed(latest))
                agent_settings = {
                    "model": task.get("agent_model") or "",
                    "reasoning_effort": task.get("agent_effort") or "",
                    "service_tier": task.get("agent_tier") or "",
                }
                task["effective_model"] = task_model(plan, agent_settings, task)
                task["effective_effort"] = task_effort(plan, agent_settings, task)
                task["effective_tier"] = task_tier(plan, agent_settings, task)
                task["contract"] = safe_json(task.get("contract_json"), {})
            orch_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 24", (orchestrator_log_id(pid),))
            orchestrator = {
                "id": orchestrator_log_id(pid),
                "status": "running" if plan.get("status") in ("planning", "preflight", "running") else plan.get("status"),
                "model": plan.get("orchestrator_used") or plan.get("orchestrator_model"),
                "reasoning_effort": plan.get("orchestrator_effort"),
                "service_tier": plan.get("orchestrator_tier"),
                "thread_id": plan.get("orchestrator_thread_id") or "",
                "generation": int(plan.get("orchestrator_generation") or 1),
                "turn_status": plan.get("orchestrator_turn_status") or "",
                "last_turn_id": plan.get("orchestrator_last_turn_id") or "",
                "last_error": plan.get("orchestrator_last_error") or "",
                "legacy_state": plan.get("legacy_orchestrator_status") or "",
                "consultations": consultations,
                "recent_logs": list(reversed(orch_logs)),
            }
            doctor_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 80", (doctor_log_id(pid),))
            doctor_logs.reverse()
            doctor_logs = latest_log_segment(doctor_logs, "preflight started")
            doctor = {
                "id": doctor_log_id(pid),
                "status": plan.get("preflight_status") or "idle",
                "report": safe_json(plan.get("preflight_json"), {}),
                "recent_logs": doctor_logs,
            }
            q = quota_status()
            return self.send_json({"plan": plan, "tasks": tasks, "orchestrator": orchestrator, "consultations": consultations, "doctor": doctor, "quota": q, "mission_usage": mission_usage(plan, q), "server_time": now()})
        if p.startswith("/api/logs/"):
            tid = p.split("/api/logs/",1)[1]
            return self.send_json({"logs": rows("SELECT * FROM logs WHERE task_id=? ORDER BY id", (tid,))})
        if p.startswith("/api/events/"):
            tid = p.split("/api/events/",1)[1]
            session = latest_agent_session(tid)
            events = rows("SELECT id,session_id,ts,event_type,item_type,payload_json FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 300", (tid,))
            events.reverse()
            for ev in events:
                ev["payload"] = safe_json(ev.pop("payload_json", "{}"), {})
            return self.send_json({"session": session, "events": events})
        if p.startswith("/api/messages/"):
            tid=p.split("/api/messages/",1)[1]
            return self.send_json({"messages":rows("SELECT * FROM task_messages WHERE task_id=? ORDER BY ts,id",(tid,))})
        if p.startswith("/api/timeline/"):
            tid = p.split("/api/timeline/", 1)[1]
            return self.send_json(timeline_for(tid))
        if p.startswith("/api/diff/"):
            tid = p.split("/api/diff/",1)[1]
            task = one("SELECT * FROM tasks WHERE id=?", (tid,))
            if not task:
                return self.send_json({"error":"task not found"},404)
            return self.send_json({"diff": task_diff(task)})
        if p.startswith("/api/plan-diff/"):
            pid = p.split("/api/plan-diff/", 1)[1]
            plan = one("SELECT * FROM plans WHERE id=?", (pid,))
            if not plan:
                return self.send_json({"error": "mission not found"}, 404)
            if plan.get("status") != "awaiting_apply":
                return self.send_json({"diff": "No pending integration diff for this mission."})
            try:
                return self.send_json({"diff": integration_patch(stored_integration_context(plan))})
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 409)
        if p == "/api/workspace/browse":
            selected = choose_workspace_folder()
            if not selected:
                return self.send_json({"ok": True, "cancelled": True})
            info = repo_info(selected)
            return self.send_json({
                "ok": True,
                "path": selected,
                "name": Path(selected).name,
                "is_git": bool(info.get("is_git")),
                "branch": info.get("branch") or "",
            })
        if p == "/api/quota":
            return self.send_json(quota_status(force=True, wait=True))
        if p.startswith("/api/docs/"):
            pid = p.split("/")[-1]
            write_mission_docs(pid)
            pl = one("SELECT mission_dir FROM plans WHERE id=?", (pid,))
            return self.send_json({"mission_dir": pl.get("mission_dir") if pl else ""})
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            data = self.body()
            if p == "/api/workspaces":
                ws = ensure_workspace(data.get("repo_path") or data.get("path") or "", data.get("name"))
                return self.send_json({"ok": True, "workspace": ws})

            if p == "/api/agents":
                aid = data.get("id") or str(uuid.uuid4())[:8]
                agent_model = (data.get("model") or "").strip()
                agent_effort = (data.get("reasoning_effort") or "").strip()
                agent_tier = (data.get("service_tier") or "").strip()
                if agent_model and agent_effort:
                    validate_runtime_config(agent_model, agent_effort, agent_tier or "default", "Worker profile")
                elif agent_tier and agent_tier not in VALID_TIERS:
                    raise ValueError(f"Worker profile: invalid speed {agent_tier}")
                execute(
                    "INSERT OR REPLACE INTO agents(id,name,role,engine,model,mode,created_at,reasoning_effort,service_tier) VALUES(?,?,?,?,?,?,COALESCE((SELECT created_at FROM agents WHERE id=?),?),?,?)",
                    (
                        aid,
                        data["name"],
                        data["role"],
                        "codex",
                        agent_model,
                        data.get("mode", "read"),
                        aid,
                        now(),
                        agent_effort,
                        agent_tier,
                    ),
                )
                return self.send_json({"ok": True, "id": aid})

            if p == "/api/demo-plan":
                goal = (data.get("goal") or "").strip()
                workspace_id = (data.get("workspace_id") or "").strip()
                ws = one("SELECT * FROM workspaces WHERE id=?", (workspace_id,)) if workspace_id else None
                if not ws:
                    workspace_raw = (data.get("workspace") or "").strip()
                    ws = ensure_workspace(workspace_raw, data.get("workspace_name"))
                    workspace_id = ws["id"]
                if not goal:
                    raise ValueError("Goal gerekli")
                orchestrator_model = data.get("orchestrator_model") or DEFAULT_ORCHESTRATOR
                worker_model = data.get("worker_model") or DEFAULT_WORKER
                orchestrator_effort = data.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT
                worker_effort = data.get("worker_effort") or DEFAULT_WORKER_EFFORT
                orchestrator_tier = data.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER
                worker_tier = data.get("worker_tier") or DEFAULT_WORKER_TIER
                validate_runtime_config(orchestrator_model, orchestrator_effort, orchestrator_tier, "Orchestrator")
                validate_runtime_config(worker_model, worker_effort, worker_tier, "Worker default")
                max_parallel = max(1, min(int(data.get("max_parallel") or 4), MAX_PARALLEL_HARD))
                pid = str(uuid.uuid4())[:8]
                saved_attachments = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved_attachments.append(save_attachment(pid, item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"]))
                attachment_paths = [a["path"] for a in saved_attachments]
                execute(
                    "INSERT INTO plans(id,goal,title,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json,demo_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, goal, deterministic_mission_title(goal), ws["repo_path"], "demo-simulator", "planning", now(), orchestrator_model, worker_model, max_parallel, str(mission_dir(pid)), "{}", orchestrator_effort, worker_effort, orchestrator_tier, worker_tier, json.dumps(RECOVERY_DEFAULTS), workspace_id, "auto", json.dumps(attachment_paths), 1),
                )
                execute("UPDATE workspaces SET last_opened_at=? WHERE id=?", (now(), workspace_id))
                write_mission_docs(pid)
                threading.Thread(target=build_demo_plan, args=(pid,), daemon=True).start()
                return self.send_json({"ok":True,"plan_id":pid,"status":"planning","demo":True})

            if p == "/api/plan":
                goal = (data.get("goal") or "").strip()
                workspace_id = (data.get("workspace_id") or "").strip()
                ws = one("SELECT * FROM workspaces WHERE id=?", (workspace_id,)) if workspace_id else None
                if not ws:
                    workspace_raw = (data.get("workspace") or "").strip()
                    ws = ensure_workspace(workspace_raw, data.get("workspace_name"))
                    workspace_id = ws["id"]
                workspace = ws["repo_path"]
                if not goal:
                    raise ValueError("Goal gerekli")
                automation_mode = data.get("automation_mode") or "auto"
                if automation_mode not in ("auto", "supervised", "manual"):
                    raise ValueError("Automation mode geçersiz")
                orchestrator_model = data.get("orchestrator_model") or DEFAULT_ORCHESTRATOR
                worker_model = data.get("worker_model") or DEFAULT_WORKER
                orchestrator_effort = data.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT
                worker_effort = data.get("worker_effort") or DEFAULT_WORKER_EFFORT
                orchestrator_tier = data.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER
                worker_tier = data.get("worker_tier") or DEFAULT_WORKER_TIER
                validate_runtime_config(orchestrator_model, orchestrator_effort, orchestrator_tier, "Orchestrator")
                validate_runtime_config(worker_model, worker_effort, worker_tier, "Worker default")
                max_parallel = max(1, min(int(data.get("max_parallel") or 4), MAX_PARALLEL_HARD))
                recovery = dict(RECOVERY_DEFAULTS)
                requested_recovery = data.get("recovery") or {}
                if isinstance(requested_recovery, dict):
                    for key in RECOVERY_DEFAULTS:
                        if key in requested_recovery:
                            recovery[key] = requested_recovery[key]
                if recovery.get("unknown_local_changes") != "ask":
                    raise ValueError("Unknown local changes policy must be 'ask'")
                if recovery.get("merge_conflicts") not in ("orchestrator", "ask"):
                    raise ValueError("Merge conflict policy invalid")
                if recovery.get("destructive_operations") != "never":
                    raise ValueError("Destructive operations must remain 'never'")
                pid = str(uuid.uuid4())[:8]
                saved_attachments = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved_attachments.append(save_attachment(pid, item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"]))
                attachment_paths = [a["path"] for a in saved_attachments]
                execute(
                    "INSERT INTO plans(id,goal,title,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid, goal, deterministic_mission_title(goal), str(Path(workspace).expanduser().resolve()), "codex-chatgpt", "planning", now(),
                        orchestrator_model, worker_model, max_parallel, str(mission_dir(pid)), "{}",
                        orchestrator_effort, worker_effort, orchestrator_tier, worker_tier,
                        json.dumps(recovery, ensure_ascii=False), workspace_id, automation_mode,
                        json.dumps(attachment_paths, ensure_ascii=False),
                    ),
                )
                execute("UPDATE workspaces SET last_opened_at=? WHERE id=?", (now(), workspace_id))
                write_mission_docs(pid)
                threading.Thread(target=build_plan, args=(pid,), daemon=True).start()
                return self.send_json({"ok": True, "plan_id": pid, "status": "planning"})

            if p.startswith("/api/run-plan/"):
                pid = p.split("/")[-1]
                plan=one("SELECT * FROM plans WHERE id=?",(pid,))
                if not plan: raise ValueError('Mission bulunamadı')
                if plan.get('status') not in ('approved','attention','waiting_for_user'):
                    raise ValueError('Planı önce review edip onayla.')
                if not claim_plan_run(pid):
                    return self.send_json({"error": "Bu mission için başka bir işlem zaten çalışıyor."}, 409)
                threading.Thread(target=run_plan, args=(pid,), kwargs={"claimed": True}, daemon=True).start()
                return self.send_json({"ok": True})

            if p.startswith("/api/mission-config/"):
                pid = p.split("/api/mission-config/", 1)[1]
                return self.send_json(mission_config(pid, data))

            if p.startswith("/api/pause-plan/"):
                pid = p.split("/api/pause-plan/", 1)[1]
                return self.send_json(pause_plan(pid, (data.get("reason") or "Paused by user").strip()))

            if p.startswith("/api/resume-plan/"):
                pid = p.split("/api/resume-plan/", 1)[1]
                return self.send_json(resume_plan(pid))

            if p.startswith("/api/reopen-plan/"):
                pid = p.split("/api/reopen-plan/", 1)[1]
                plan = one("SELECT * FROM plans WHERE id=?", (pid,))
                if not plan:
                    raise ValueError("Mission bulunamadı")
                if plan.get("status") != "done":
                    raise ValueError("Yalnızca tamamlanmış mission yeniden açılabilir")
                has_tasks = bool(one("SELECT id FROM tasks WHERE plan_id=? LIMIT 1", (pid,)))
                if not has_tasks:
                    execute(
                        """UPDATE plans SET status='planning',error='',decision='',decision_reason='',
                           evidence_json='[]',questions_json='[]',final_response='',summary='',
                           pending_question_id='',pending_question_json='{}',started_at=NULL,
                           finished_at=NULL WHERE id=?""",
                        (pid,),
                    )
                    log(orchestrator_log_id(pid), "manual", "no-task mission reopened; re-evaluating disposition in the same orchestrator conversation")
                    write_mission_docs(pid)
                    if claim_plan_run(pid):
                        threading.Thread(target=build_plan, args=(pid,), daemon=True).start()
                    return self.send_json({"ok": True, "status": "planning"})
                execute("UPDATE plans SET status=?,error='',finished_at=NULL WHERE id=?", ("approved", pid))
                log(orchestrator_log_id(pid), "manual", "mission reopened by user; completed tasks remain checkpoints")
                write_mission_docs(pid)
                if claim_plan_run(pid):
                    threading.Thread(target=run_plan, args=(pid,), kwargs={"claimed": True}, daemon=True).start()
                return self.send_json({"ok": True, "status": "resuming"})

            if p.startswith("/api/restart-plan/"):
                pid = p.split("/api/restart-plan/", 1)[1]
                return self.send_json(restart_as_new_mission(pid))

            if p.startswith("/api/pause-task/"):
                tid = p.split("/api/pause-task/", 1)[1]
                return self.send_json(pause_task(tid))

            if p.startswith("/api/resume-task/"):
                tid = p.split("/api/resume-task/", 1)[1]
                return self.send_json(resume_task(tid))

            if p.startswith("/api/reconsider-plan/"):
                pid = p.split("/api/reconsider-plan/", 1)[1]
                mode = (data.get("mode") or "reconsider").strip()
                note = (data.get("note") or "").strip()
                return self.send_json(replan_mission(pid, mode=mode, user_note=note))

            if p.startswith("/api/consultation-answer/"):
                pid = p.split("/api/consultation-answer/", 1)[1]
                consultation_id = (data.get("consultation_id") or "").strip()
                if not consultation_id:
                    raise ValueError("Consultation id gerekli")
                consultation = one(
                    "SELECT task_id FROM consultations WHERE id=? AND plan_id=?",
                    (consultation_id, pid),
                )
                if not consultation:
                    raise ValueError("Consultation bulunamadı")
                saved = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved.append(
                            save_attachment(
                                pid,
                                item.get("name") or "consultation.png",
                                item.get("mime") or "image/png",
                                item["data_base64"],
                                task_id=consultation["task_id"],
                            )
                        )
                answer = (data.get("answer") or "").strip()
                option = (data.get("option") or "").strip()
                if option:
                    answer = f"{option}\n{answer}".strip()
                return self.send_json(
                    answer_consultation(pid, consultation_id, answer, [x["path"] for x in saved])
                )

            if p.startswith("/api/reconstruct-orchestrator/"):
                pid = p.split("/api/reconstruct-orchestrator/", 1)[1]
                plan = one("SELECT * FROM plans WHERE id=?", (pid,))
                if not plan:
                    raise ValueError("Mission bulunamadı")
                if not claim_plan_run(pid):
                    return self.send_json({"error": "Bu mission için başka bir işlem zaten çalışıyor."}, 409)
                threading.Thread(
                    target=lambda: _run_reconstruct_and_release(pid),
                    daemon=True,
                ).start()
                return self.send_json({"ok": True, "status": "reconstructing"})

            if p.startswith("/api/preflight-action/"):
                pid = p.split("/api/preflight-action/", 1)[1]
                return self.send_json(apply_preflight_action(pid, data.get("action"), data.get("paths") or []))

            if p.startswith("/api/run-task/"):
                return self.send_json({"error": "Direct task execution is disabled; start the approved mission instead."}, 410)

            if p.startswith("/api/apply-plan/"):
                pid = p.split("/api/apply-plan/", 1)[1]
                result = apply_plan(pid)
                return self.send_json(result)

            if p.startswith("/api/follow-up/"):
                tid = p.split("/api/follow-up/",1)[1]
                prompt = (data.get("prompt") or "").strip()
                if not prompt and not data.get("attachments"):
                    raise ValueError("Mesaj veya attachment gerekli")
                task = one("SELECT * FROM tasks WHERE id=?", (tid,))
                if not task:
                    raise ValueError("Task bulunamadı")
                image_paths=[]
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved=save_attachment(task["plan_id"], item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"], task_id=tid)
                        image_paths.append(saved["path"])
                mid=str(uuid.uuid4())[:10]
                with RUNNERS_LOCK:
                    is_running=bool(RUNNERS.get(tid))
                with APP_SERVER_CONTROLS_LOCK:
                    app_server_active = tid in APP_SERVER_CONTROLS
                status='sending' if app_server_active else ('queued' if is_running or task.get('status') in ('running','executed') else 'sending')
                execute("INSERT INTO task_messages(id,task_id,plan_id,ts,text,attachments_json,status) VALUES(?,?,?,?,?,?,?)", (mid,tid,task['plan_id'],now(),prompt,json.dumps(image_paths),status))
                if status=='sending':
                    if app_server_active:
                        if not steer_app_server(tid, mid, prompt, image_paths):
                            # The active turn may have completed between the
                            # availability check and the steer request. The
                            # helper records that race as a failed message so
                            # it is visible instead of being silently lost.
                            pass
                    else:
                        threading.Thread(target=run_manual_followup_message,args=(mid,),daemon=True).start()
                else:
                    log(tid,'manual','user message queued for the next Codex turn')
                return self.send_json({"ok":True,"queued":status=='queued',"message_id":mid})

            if p.startswith("/api/orchestrator-follow-up/"):
                pid=p.split("/api/orchestrator-follow-up/",1)[1]
                prompt=(data.get('prompt') or '').strip()
                if not prompt:
                    raise ValueError('Mesaj gerekli')
                threading.Thread(target=run_orchestrator_followup,args=(pid,prompt,[]),daemon=True).start()
                return self.send_json({'ok':True})

            if p.startswith("/api/task-config/"):
                tid=p.split("/api/task-config/",1)[1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                if plan.get('status') not in ('planned','approved'):
                    raise ValueError('Task assignment yalnızca execution başlamadan önce değiştirilebilir.')
                agent_id=(data.get('agent_id') or task.get('agent_id') or '').strip()
                if agent_id and not one("SELECT id FROM agents WHERE id=?",(agent_id,)):
                    raise ValueError('Agent profile bulunamadı')
                mode=data.get('mode') or task.get('mode') or 'read'
                if mode not in ('read','write'): raise ValueError('Geçersiz task mode')
                execute("UPDATE tasks SET agent_id=?,mode=? WHERE id=?",(agent_id,mode,tid))
                if plan.get('status')=='approved':
                    execute("UPDATE plans SET status=?,approved_at=NULL WHERE id=?",('planned',plan['id']))
                write_mission_docs(plan['id'])
                return self.send_json({'ok':True})

            if p.startswith("/api/approve-plan/"):
                pid=p.split("/api/approve-plan/",1)[1]
                plan=one("SELECT * FROM plans WHERE id=?",(pid,))
                if not plan: raise ValueError('Mission bulunamadı')
                if plan.get('status')!='planned': raise ValueError('Plan review için hazır değil')
                tasks=rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq",(pid,))
                if not tasks: raise ValueError('Onaylanacak task yok')
                missing=[t['title'] for t in tasks if not t.get('agent_id')]
                if missing: raise ValueError('Agent atanmamış task var: '+', '.join(missing))
                execute("UPDATE plans SET status=?,approved_at=?,approval_note=? WHERE id=?",('approved',now(),(data.get('note') or '').strip(),pid))
                log(orchestrator_log_id(pid),'supervisor','plan approved by user · execution is still paused until Start mission')
                write_mission_docs(pid)
                return self.send_json({'ok':True})

            if p.startswith("/api/open-terminal/"):
                tid=p.split("/api/open-terminal/",1)[1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                open_terminal_at(task.get('workspace') or plan.get('workspace'))
                return self.send_json({'ok':True})

            if p.startswith("/api/cancel-task/"):
                tid = p.split("/")[-1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                with RUNNERS_LOCK:
                    proc = RUNNERS.get(tid)
                if proc:
                    terminate_process(proc)
                    execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("cancelled", "User cancelled", now(), tid))
                    return self.send_json({"ok": True})
                if plan and int(plan.get('demo_mode') or 0) and task.get('status')=='running':
                    execute("UPDATE tasks SET status=?,error=?,finished_at=? WHERE id=?",('cancelled','User cancelled demo agent',now(),tid))
                    return self.send_json({'ok':True})
                return self.send_json({"ok": False, "message": "Task çalışmıyor"}, 409)

            return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)


def main():
    init_db()
    url = f"http://{HOST}:{PORT}"
    print(f"AgentDock v0.12 running at {url}")
    print("Auth mode: Codex CLI signed in with ChatGPT (Plus supported).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
