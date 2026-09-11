#!/usr/bin/env python3
import concurrent.futures
import base64
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


def planner_schema_path():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATE_ROOT / "planner.schema.json"
    expected = json.dumps(PLANNER_SCHEMA, ensure_ascii=False, indent=2) + "\n"
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
    ]:
        ensure_column("tasks", name, ddl)

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


def recover_orphaned_runs():
    """Move work interrupted by a server restart into an explicit retry state."""
    interrupted_at = now()
    with DB_LOCK:
        con = db()
        plans = con.execute(
            "SELECT id FROM plans WHERE status IN ('preflight','running')"
        ).fetchall()
        if not plans:
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
            f"UPDATE tasks SET status='attention', error=?, finished_at=? "
            f"WHERE plan_id IN ({placeholders}) AND status IN ('running','executed')",
            (message, interrupted_at, *plan_ids),
        )
        con.execute(
            f"UPDATE agent_sessions SET status='interrupted', finished_at=? "
            f"WHERE plan_id IN ({placeholders}) AND status='running'",
            (interrupted_at, *plan_ids),
        )
        con.commit()
        con.close()
    for plan_id in plan_ids:
        log(orchestrator_log_id(plan_id), "supervisor", message)
        write_mission_docs(plan_id)
    return len(plan_ids)


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
    plans = rows("SELECT id,goal,status,created_at,error,max_parallel,worker_model,orchestrator_model FROM plans WHERE workspace_id=? ORDER BY created_at DESC", (wid,))
    running = queued = attention = done = 0
    for pl in plans:
        stats = one("""SELECT
            SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) queued,
            SUM(CASE WHEN status IN ('failed','blocked','cancelled') THEN 1 ELSE 0 END) issues,
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
    return obj


def finish_agent_session(session_id, status, final_response=""):
    execute("UPDATE agent_sessions SET status=?, finished_at=?, final_response=? WHERE id=?",
            (status, now(), str(final_response or "")[-50000:], session_id))


def latest_agent_session(task_id):
    return one("SELECT * FROM agent_sessions WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,))


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


def shell(args, cwd=None, input_text=None, check=True, timeout=None):
    p = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"exit {p.returncode}")[-12000:])
    return p


def git(cwd, *args, check=True, input_text=None):
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    return shell([exe, "-C", str(cwd), *args], input_text=input_text, check=check)


def repo_info(workspace):
    workspace = Path(workspace).expanduser().resolve()
    try:
        root = Path(git(workspace, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        head = git(root, "rev-parse", "HEAD").stdout.strip()
        rel = workspace.relative_to(root)
        dirty = bool(git(root, "status", "--porcelain").stdout.strip())
        branch = git(root, "branch", "--show-current", check=False).stdout.strip()
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
    raw = git(repo_root, "rev-parse", arg).stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(repo_root) / p).resolve()
    return p


def git_status_entries(repo_root):
    out = git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
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
    names = [x.strip() for x in git(repo_root, "remote", check=False).stdout.splitlines() if x.strip()]
    remotes = []
    for name in names:
        fetch = [x.strip() for x in git(repo_root, "config", "--get-all", f"remote.{name}.url", check=False).stdout.splitlines() if x.strip()]
        push = [x.strip() for x in git(repo_root, "config", "--get-all", f"remote.{name}.pushurl", check=False).stdout.splitlines() if x.strip()]
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
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).stdout.strip()
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
        dry = git(repo_root, "worktree", "prune", "--dry-run", check=False).stdout.strip()
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
            report["blockers"].append(f"Git lock requires explicit resolution ({owner}): {lock}")

        entries = git_status_entries(repo_root)
        tracked = [f"{e['xy']} {e['path']}" for e in entries if e["xy"] != "??"]
        unknown = [e["path"] for e in entries if e["xy"] == "??"]
        affected = [e["path"] for e in entries]
        report["affected_paths"] = affected[:100]
        if has_write and affected:
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


def reset_plan_for_retry(plan):
    plan_id = plan["id"]
    log(orchestrator_log_id(plan_id), "supervisor", "retry requested; resetting task runtime state while preserving contracts")
    info = repo_info(plan["workspace"])
    if info["is_git"]:
        repo_root = Path(info["root"])
        base_dir, integration_dir = plan_paths(plan_id)
        remove_worktree(repo_root, integration_dir)
        # Remove any task worktrees registered under this plan.
        if base_dir.exists():
            for child in list(base_dir.iterdir()):
                if child == integration_dir:
                    continue
                remove_worktree(repo_root, child)
        for t in rows("SELECT branch FROM tasks WHERE plan_id=?", (plan_id,)):
            if t.get("branch"):
                delete_branch(repo_root, t["branch"])
        delete_branch(repo_root, f"agentdock/{plan_id}/integration")
        git(repo_root, "worktree", "prune", check=False)
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)
    execute(
        "UPDATE tasks SET status='pending', output='', error='', started_at=NULL, finished_at=NULL, workspace='', branch='', commit_hash='', integration_status='', retry_count=0, last_failure_kind='', repair_count=0 WHERE plan_id=?",
        (plan_id,),
    )
    execute("UPDATE plans SET base_commit='', integration_workspace='', summary='', applied=0, error='', apply_status='', apply_error='', started_at=NULL, finished_at=NULL WHERE id=?", (plan_id,))
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
    del service_tier  # App Server currently receives the model/effort controls used here.
    workspace = str(Path(workspace).expanduser().resolve())
    if not Path(workspace).is_dir():
        raise RuntimeError(f"Workspace bulunamadı: {workspace}")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    plan_id = infer_plan_id(task_id)
    session_id = create_agent_session(plan_id, task_id or "", session_kind, model, reasoning_effort, "default", mode, workspace)
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
            "clientInfo": {"name": "agentdock", "title": "AgentDock", "version": "0.11.0"},
            "capabilities": {"experimentalApi": True},
        })
        send({"method": "initialized", "params": {}})
        thread_params = {"model": model or DEFAULT_WORKER, "cwd": workspace, "serviceName": "agentdock"}
        if resume_thread_id:
            thread_result = request(2, "thread/resume", {"threadId": resume_thread_id})
        else:
            thread_result = request(2, "thread/start", thread_params)
        thread = thread_result.get("thread") if isinstance(thread_result, dict) else {}
        thread_id = str((thread or {}).get("id") or resume_thread_id or "")
        if thread_id:
            execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (thread_id, session_id))

        turn_params = {
            "threadId": thread_id,
            "input": app_server_input_items(prompt, images, task_id),
            "cwd": workspace,
            "model": model or DEFAULT_WORKER,
            "effort": reasoning_effort or DEFAULT_WORKER_EFFORT,
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
        if task_id and thread_id and turn_id:
            with APP_SERVER_CONTROLS_LOCK:
                APP_SERVER_CONTROLS[task_id] = {
                    "send": send,
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
        cancelled = bool(task_id and (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") == "cancelled")
        finish_agent_session(session_id, "cancelled" if cancelled else "failed", str(exc))
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
        cancelled = bool(task_id and (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") == "cancelled")
        finish_agent_session(session_id, "cancelled" if cancelled else "failed", final)
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


def orchestrator_models(requested):
    if requested == "auto-best":
        return ["gpt-6-astra", "gpt-5.6-sol"]
    return [requested or DEFAULT_ORCHESTRATOR]


def run_orchestrator(prompt, workspace, requested_model, task_id=None, reasoning_effort="", service_tier="default", mode="read", transient_retries=0, images=None, output_schema=""):
    errors = []
    for model in orchestrator_models(requested_model):
        attempt = 0
        while True:
            try:
                return run_codex(prompt, workspace, mode, model, task_id, reasoning_effort, service_tier, images=images, session_kind="orchestrator", output_schema=output_schema), model
            except Exception as e:
                err = str(e)
                if attempt < transient_retries and is_transient_error(err):
                    attempt += 1
                    delay = 2 if attempt == 1 else 5
                    log(task_id, "supervisor", f"self-heal: orchestrator transient failure on {model}; retry {attempt}/{transient_retries} in {delay}s")
                    time.sleep(delay)
                    continue
                errors.append(f"{model}: {e}")
                break
        if requested_model != "auto-best":
            break
    raise RuntimeError("\n\n".join(errors))


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
    return {
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "final_response": final_response,
        "questions": questions,
        "tasks": tasks,
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

DEPENDENCY RESULTS:
{dep_context or 'None.'}

NON-NEGOTIABLE WORK RULES:
- Execute only this contract. Do not expand scope or redesign adjacent systems.
- Stay inside the provided workspace and the contract's allowed_paths.
- You are one parallel worker. Do not coordinate via Git branches/commits; the harness handles isolation and integration.
- Do not make architecture, product, scope, prioritization, dependency, or destructive-operation decisions.
- Low-level implementation choices are allowed only when the contract's decision_policy permits them and all stated interfaces/invariants remain unchanged.
- If a reserved decision or material ambiguity is required, STOP before guessing and return exactly `BLOCKED_NEEDS_ORCHESTRATOR:` followed by the missing decision and 1-3 concrete options/evidence.
- Do not claim a verification passed unless you actually ran or inspected it.
- If the task is read-only, do not modify files.
- Finish with a concise result covering deliverables, files changed/findings, verification performed, and remaining risks.
"""


def task_model(plan, agent):
    return (agent.get("model") or "").strip() or plan.get("worker_model") or DEFAULT_WORKER


def task_effort(plan, agent):
    return (agent.get("reasoning_effort") or "").strip() or plan.get("worker_effort") or DEFAULT_WORKER_EFFORT


def task_tier(plan, agent):
    return (agent.get("service_tier") or "").strip() or plan.get("worker_tier") or DEFAULT_WORKER_TIER


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
        raise RuntimeError("Preflight sonrası working tree yeniden değişti. AgentDock kullanıcı çalışmasını ezmemek için paralel write başlatmadı.")
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


def validate_read_workspace(workspace, expected_head):
    current_head = git(workspace, "rev-parse", "HEAD", check=False).stdout.strip()
    dirty = git(workspace, "status", "--porcelain=v1", "--untracked-files=all", check=False).stdout.strip()
    if current_head != expected_head or dirty:
        raise RuntimeError("Read-only task workspace'i değiştirdi; değişiklik güvenlik için entegre edilmedi.")


def run_task_once(plan, task, workspace, force_mode=None):
    agent = one("SELECT * FROM agents WHERE id=?", (task["agent_id"],)) or one("SELECT * FROM agents ORDER BY created_at LIMIT 1")
    mode = force_mode or task["mode"]
    model = task_model(plan, agent)
    effort = task_effort(plan, agent)
    tier = task_tier(plan, agent)
    execute("UPDATE tasks SET status=?, started_at=?, error=?, workspace=? WHERE id=?", ("running", now(), "", str(workspace), task["id"]))
    write_mission_docs(plan["id"])
    try:
        output = run_codex(make_task_prompt(plan, task, agent), workspace, mode, model, task["id"], effort, tier, images=plan_attachment_paths(plan), session_kind="worker")
        if "BLOCKED_NEEDS_ORCHESTRATOR:" in output:
            execute("UPDATE tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?", ("blocked", output, "Worker escalated a reserved decision to the orchestrator", now(), task["id"]))
            write_mission_docs(plan["id"])
            return {"ok": False, "blocked": True, "output": output, "error": "Worker needs orchestrator decision"}
        followups = consume_queued_messages(plan, task, workspace, model, effort, tier)
        if followups:
            output = followups[-1]
        execute("UPDATE tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?", ("executed", output, "", now(), task["id"]))
        write_mission_docs(plan["id"])
        return {"ok": True, "output": output}
    except Exception as e:
        current=one("SELECT status FROM tasks WHERE id=?",(task["id"],)) or {}
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
        if result.get("ok") or result.get("blocked"):
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
    if task["mode"] == "write":
        try:
            wt, workspace, branch = create_worker_worktree(ctx, plan, task, wave_base_commit)
        except Exception as e:
            execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), now(), task["id"]))
            return {"task": task, "ok": False, "write": True, "phase": "worktree", "error": str(e)}
        result = run_task_with_recovery(plan, task, workspace)
        commit_hash = ""
        if result["ok"]:
            try:
                changed = validate_worker_changes(wt, task)
                log(task["id"], "supervisor", f"contract path check passed · files={len(changed)}")
                commit_hash = commit_worker_changes(wt, task)
                execute("UPDATE tasks SET commit_hash=? WHERE id=?", (commit_hash, task["id"]))
            except Exception as e:
                result = {"ok": False, "phase": "contract", "error": f"Worker değişiklikleri contract kontrolünden geçemedi: {e}"}
                execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", result["error"], now(), task["id"]))
        return {"task": task, "ok": result["ok"], "write": True, "phase": "worker", "wt": str(wt), "branch": branch, "commit": commit_hash, **result}
    else:
        expected_head = None
        if ctx.get("base_commit") and repo_info(ctx["integration_workspace"]).get("is_git"):
            expected_head = git(ctx["integration_workspace"], "rev-parse", "HEAD", check=False).stdout.strip()
        result = run_task_with_recovery(plan, task, ctx["integration_workspace"], force_mode="read")
        if result.get("ok") and expected_head is not None:
            try:
                validate_read_workspace(ctx["integration_workspace"], expected_head)
            except Exception as e:
                result = {"ok": False, "phase": "contract", "error": str(e)}
                execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), now(), task["id"]))
        return {"task": task, "ok": result["ok"], "write": False, "phase": "worker", **result}



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
        text, used = run_orchestrator(
            merge_conflict_prompt(plan, task, unresolved, cherry_error),
            ctx["integration_workspace"],
            plan["orchestrator_model"],
            orchestrator_log_id(plan["id"]),
            plan["orchestrator_effort"],
            plan["orchestrator_tier"],
            mode="write",
            transient_retries=retries,
        )
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
        text, used_model = run_orchestrator(
            prompt, plan["workspace"], plan["orchestrator_model"], orchestrator_log_id(plan_id),
            plan["orchestrator_effort"], plan["orchestrator_tier"],
            transient_retries=1 if recovery.get("auto_retry_transient") else 0,
            images=plan_attachment_paths(plan),
            output_schema=planner_schema_path(),
        )
        obj = normalize_planner_result(extract_json(text))
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
                "UPDATE plans SET decision=?,decision_reason=?,evidence_json=?,questions_json=?,final_response=?,summary=?,error=?,status=?,orchestrator_used=?,finished_at=? WHERE id=?",
                (
                    disposition, obj["reason"], json.dumps(obj["evidence"], ensure_ascii=False),
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
            "UPDATE plans SET decision=?,decision_reason=?,evidence_json=?,questions_json=?,final_response=?,status=?,orchestrator_used=?,error=?,finished_at=NULL WHERE id=?",
            (
                disposition, obj["reason"], json.dumps(obj["evidence"], ensure_ascii=False),
                json.dumps(obj["questions"], ensure_ascii=False), obj["final_response"], "planned", used_model, "", plan_id,
            ),
        )
        log(orchestrator_log_id(plan_id), "supervisor", f"execution plan ready · {len(items)} task(s)")
        write_mission_docs(plan_id)
    except Exception as e:
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
    model = task_model(plan, agent)
    effort = task_effort(plan, agent)
    tier = task_tier(plan, agent)
    started = now()
    execute("UPDATE tasks SET status=?,started_at=?,error=? WHERE id=?", ("running", started, "", task_id))
    sid = create_agent_session(plan["id"], task_id, "demo-worker", model, effort, tier, task.get("mode") or "read", plan["workspace"])
    fake_thread = f"demo-{task_id}-{sid[:6]}"
    demo_event(sid, task_id, plan["id"], {"type":"thread.started","thread_id":fake_thread})
    demo_event(sid, task_id, plan["id"], {"type":"turn.started"})
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
    if (one("SELECT status FROM tasks WHERE id=?",(task_id,)) or {}).get("status")=="cancelled":
        finish_agent_session(sid,"cancelled","User stopped demo agent")
        log(task_id,"manual","demo agent stopped by user")
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
    if (one("SELECT status FROM tasks WHERE id=?",(task_id,)) or {}).get("status")=="cancelled":
        finish_agent_session(sid,"cancelled","User stopped demo agent")
        log(task_id,"manual","demo agent stopped by user")
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
        demo_event(sid, oid, plan_id, {"type":"turn.started"})
        demo_event(sid, oid, plan_id, {"type":"item.completed","item":{"id":"orch-r1","type":"reasoning","text":"I will propose explicit task boundaries and dependencies first. Execution will not start until the user reviews assignments and approves the plan."}})
        log(oid, "supervisor", "building preview execution contracts")
        time.sleep(0.55)
        goal = plan["goal"]
        insert_demo_task(plan_id, 0, "Inspect scope and affected surfaces", "architect", "read", [], demo_contract(goal, "Architect", "Inspect the requested outcome, identify likely affected surfaces and define safe implementation boundaries.", ["workspace/** (read-only)"], ["Inspect repository structure", "Locate likely affected files", "Summarize constraints for workers"], ["Affected surfaces are explicit", "No implementation changes are made"], ["git status --short", "rg relevant symbols"]))
        insert_demo_task(plan_id, 1, "Implement the primary change", "coder", "write", [0], demo_contract(goal, "Coder", "Implement the primary requested change inside the orchestrator-defined scope.", ["src/**", "tests/**"], ["Read architect findings", "Apply the smallest focused change", "Run targeted verification"], ["Requested behavior is implemented", "Diff stays focused", "Targeted checks pass"], ["git diff --check", "targeted test command"]))
        insert_demo_task(plan_id, 2, "Add focused verification", "tester", "write", [0], demo_contract(goal, "Tester", "Add or update the smallest meaningful verification for the requested outcome.", ["tests/**", "src/** only when test fixtures require it"], ["Identify the regression boundary", "Add focused coverage", "Run the relevant test slice"], ["Coverage demonstrates the requested behavior", "Verification passes"], ["targeted test command"]))
        insert_demo_task(plan_id, 3, "Review the integrated result", "reviewer", "read", [1,2], demo_contract(goal, "Reviewer", "Independently review the integrated result for scope compliance, regressions and missing verification.", ["workspace/** (read-only)"], ["Inspect integrated diff", "Check acceptance criteria", "Report residual risks"], ["No critical regression is found", "Verification result is explicit"], ["git diff --check", "test summary"]))
        execute("UPDATE plans SET status=?,decision=?,decision_reason=?,evidence_json=?,final_response=?,orchestrator_used=? WHERE id=?", ("planned", "execute", "Demo preview intentionally exercises the execution path.", json.dumps(["Local demo simulator selected; no Codex worker was called."], ensure_ascii=False), "", plan["orchestrator_model"], plan_id))
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
        log(oid, "supervisor", "launching approved parallel preview wave: TASK-002 + TASK-003")
        th1=threading.Thread(target=simulate_demo_task,args=(plan,byseq[1]['id'],"The contract is explicit, so I can implement the focused change without making product or architecture decisions.","git diff --check"),kwargs={"file_changes":[{"path":"src/example.py","kind":"update"}],"duration":4.0},daemon=True)
        th2=threading.Thread(target=simulate_demo_task,args=(plan,byseq[2]['id'],"I am adding the smallest regression check that proves the requested behavior while avoiding unrelated coverage expansion.","python3 -m unittest discover -s tests"),kwargs={"file_changes":[{"path":"tests/test_example.py","kind":"add"}],"duration":4.0},daemon=True)
        th1.start(); th2.start(); th1.join(); th2.join()
        log(oid,'supervisor','parallel wave complete · starting independent review')
        simulate_demo_task(plan,byseq[3]['id'],"I am reviewing the simulated integrated result against the execution contracts and verification evidence.","git diff --check && python3 -m unittest discover -s tests",duration=2.5)
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
    old_status = task.get("status") or "done"
    execute("UPDATE tasks SET status=?,error=? WHERE id=?", ("running", "", task_id))
    sid = create_agent_session(plan["id"], task_id, "demo-manual", plan["worker_model"], plan["worker_effort"], plan["worker_tier"], task.get("mode") or "read", plan["workspace"])
    demo_event(sid, task_id, plan["id"], {"type":"thread.started","thread_id":f"demo-followup-{task_id}-{sid[:6]}"})
    demo_event(sid, task_id, plan["id"], {"type":"turn.started"})
    log(task_id, "manual", f"demo manual input: {prompt or '[attachment only]'}")
    time.sleep(0.4)
    note = "I received your manual instruction in Demo Mode. In a real mission this would continue the same Codex thread and apply your steering to the task."
    if image_paths:
        note += f" {len(image_paths)} image attachment(s) were accepted."
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"manual-reason","type":"reasoning","text":"The user has taken manual control, so I will prioritize the new instruction over the original execution preference while keeping the task contract boundaries."}})
    demo_event(sid, task_id, plan["id"], {"type":"item.completed","item":{"id":"manual-msg","type":"agent_message","text":note}})
    demo_event(sid, task_id, plan["id"], {"type":"turn.completed","usage":{"input_tokens":0,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0}})
    finish_agent_session(sid, "completed", note)
    final_status = "done" if old_status in ("done","executed","failed","blocked","cancelled") else old_status
    execute("UPDATE tasks SET status=?,output=?,finished_at=? WHERE id=?", (final_status, note, now(), task_id))
    log(task_id, "manual", "demo manual follow-up completed · 0 quota used")

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
    if result.get("write") and result.get("wt") and ctx:
        remove_worktree(ctx["repo_root"], result["wt"])
        if result.get("branch"):
            delete_branch(ctx["repo_root"], result["branch"])
    log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} escalated a reserved decision; root orchestrator is resolving it")
    try:
        text, used = run_orchestrator(
            escalation_prompt(plan, fresh, result.get("output") or result.get("error") or ""),
            workspace, plan["orchestrator_model"], orchestrator_log_id(plan["id"]),
            plan["orchestrator_effort"], plan["orchestrator_tier"],
            transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
        )
        obj = extract_json(text)
        if obj.get("action") == "retry" and isinstance(obj.get("contract"), dict):
            execute(
                "UPDATE tasks SET contract_json=?, instructions=?, status=?, error=?, escalation_count=? WHERE id=?",
                (json.dumps(obj["contract"], ensure_ascii=False), obj["contract"].get("objective") or fresh.get("instructions") or "", "pending", "", count + 1, task["id"]),
            )
            log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} contract revised by {used}; worker will retry without making the decision")
            write_mission_docs(plan["id"])
            return True
        reason = obj.get("reason") or "Orchestrator chose not to retry this escalation."
        execute("UPDATE tasks SET status=?, error=?, escalation_count=? WHERE id=?", ("blocked", reason, count + 1, task["id"]))
        log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} remains blocked: {reason}")
        write_mission_docs(plan["id"])
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
Return ONLY JSON:
{{"action":"retry","note":"brief diagnosis","contract":{{...complete revised contract...}}}}
or
{{"action":"stop","reason":"why automatic recovery is unsafe or cannot help"}}

Rules:
- Do not broaden scope, change product behavior, add dependencies, alter public APIs, or make destructive changes.
- A retry contract must be complete and more concrete: exact steps, paths, acceptance criteria and verification.
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
    if result.get("write") and result.get("wt") and ctx:
        remove_worktree(ctx["repo_root"], result["wt"])
        if result.get("branch"):
            delete_branch(ctx["repo_root"], result["branch"])
    log(orchestrator_log_id(plan["id"]), "supervisor", f"TASK-{task['seq']+1:03d} failed; root orchestrator is diagnosing one bounded recovery attempt")
    execute("UPDATE plans SET recovery_count=recovery_count+1 WHERE id=?", (plan["id"],))
    try:
        text, used = run_orchestrator(
            failure_recovery_prompt(plan, fresh, result),
            ctx["integration_workspace"] if ctx else plan["workspace"],
            plan["orchestrator_model"], orchestrator_log_id(plan["id"]),
            plan["orchestrator_effort"], plan["orchestrator_tier"],
            transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
        )
        obj = extract_json(text)
        if obj.get("action") == "retry" and isinstance(obj.get("contract"), dict):
            contract = obj["contract"]
            execute(
                "UPDATE tasks SET contract_json=?, instructions=?, status='pending', error='', output='', started_at=NULL, finished_at=NULL, repair_count=? WHERE id=?",
                (json.dumps(contract, ensure_ascii=False), contract.get("objective") or fresh.get("instructions") or "", count + 1, task["id"]),
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
        if plan.get("status") == "attention":
            reset_plan_for_retry(plan)
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
        execute("UPDATE plans SET status=? WHERE id=?", ("running", plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", "mission execution started")
        write_mission_docs(plan_id)
        if has_write:
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
            ready = ready_tasks(plan_id, pending)
            if not ready:
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

            # Integrate only after the whole wave has completed, so workers truly run from the same snapshot.
            for result in sorted(results, key=lambda r: r["task"]["seq"]):
                task = result["task"]
                if result.get("cancelled"):
                    log(orchestrator_log_id(plan_id), "supervisor", f"TASK-{task['seq']+1:03d} stopped by user; mission will require attention")
                    pending.pop(task["seq"], None)
                    continue
                if result.get("blocked"):
                    retry = resolve_worker_escalation(plan, task, result, ctx["integration_workspace"], ctx)
                    if retry:
                        pending[task["seq"]] = one("SELECT * FROM tasks WHERE id=?", (task["id"],))
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

        task_rows = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))
        all_done = bool(task_rows) and all(t["status"] in ("done", "executed") for t in task_rows)

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

        synth_workspace = ctx["integration_workspace"] if has_write else Path(plan["workspace"])
        try:
            log(orchestrator_log_id(plan_id), "supervisor", "starting final orchestrator synthesis")
            summary, used_model = run_orchestrator(
                synthesis_prompt(plan, task_rows), synth_workspace, plan["orchestrator_model"],
                orchestrator_log_id(plan_id), plan["orchestrator_effort"], plan["orchestrator_tier"],
                transient_retries=1 if recovery_settings(plan).get("auto_retry_transient") else 0,
            )
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
        end_usage = quota_status(force=True, wait=True)
        execute("UPDATE plans SET status=?, error=?, apply_status=?, apply_error=?, usage_end_json=?, finished_at=? WHERE id=?", ("attention", str(e), "failed" if plan.get("status") == "awaiting_apply" else "", str(e), json.dumps(end_usage), now(), plan_id))
        log(orchestrator_log_id(plan_id), "supervisor", f"mission attention: {e}")
        write_mission_docs(plan_id)
    finally:
        release_plan_run(plan_id)


def latest_orchestrator_session(plan_id):
    return one("SELECT * FROM agent_sessions WHERE plan_id=? AND kind LIKE '%orchestrator%' ORDER BY started_at DESC LIMIT 1", (plan_id,))


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
    sess=latest_orchestrator_session(plan_id)
    thread_id=(sess or {}).get('thread_id') or ''
    if not thread_id:
        raise ValueError('Orchestrator conversation is not resumable yet.')
    text, used=run_orchestrator(prompt, plan['workspace'], plan['orchestrator_model'], orchestrator_log_id(plan_id), plan['orchestrator_effort'], plan['orchestrator_tier'], mode='read', images=image_paths or []) if not thread_id else (run_codex(prompt, plan['workspace'], 'read', plan.get('orchestrator_used') or plan['orchestrator_model'], orchestrator_log_id(plan_id), plan['orchestrator_effort'], plan['orchestrator_tier'], images=image_paths or [], resume_thread_id=thread_id, session_kind='orchestrator-manual'), plan.get('orchestrator_used') or plan['orchestrator_model'])
    log(orchestrator_log_id(plan_id), 'manual', 'manual orchestrator follow-up completed')


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
    thread_id = (latest or {}).get("thread_id") or ""
    workspace = task.get("workspace") or ""
    if not workspace or not Path(workspace).is_dir():
        raise ValueError("Bu task'ın izole worktree'si artık mevcut değil. Yeni bir mission/task açarak devam et.")
    agent = one("SELECT * FROM agents WHERE id=?", (task.get("agent_id"),)) or {}
    model = task_model(plan, agent)
    effort = task_effort(plan, agent)
    tier = task_tier(plan, agent)
    old_status = task.get("status") or "done"
    execute("UPDATE tasks SET status=?, error=? WHERE id=?", ("running", "", task_id))
    log(task_id, "manual", "manual control: user follow-up started")
    try:
        out = run_codex(prompt, workspace, task.get("mode") or "read", model, task_id, effort, tier,
                        images=image_paths or [], resume_thread_id=thread_id, session_kind="manual")
        final_status = "done" if old_status in ("done", "executed", "cancelled", "failed", "blocked") else old_status
        execute("UPDATE tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?", (final_status, out, "", now(), task_id))
        log(task_id, "manual", "manual control: follow-up completed")
        write_mission_docs(plan["id"])
    except Exception as e:
        execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("attention", str(e), now(), task_id))
        log(task_id, "manual", f"manual control failed: {e}")
        write_mission_docs(plan["id"])


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
                "version": "0.11.0",
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
                task["effective_model"] = (task.get("agent_model") or "").strip() or plan.get("worker_model") or DEFAULT_WORKER
                task["effective_effort"] = (task.get("agent_effort") or "").strip() or plan.get("worker_effort") or DEFAULT_WORKER_EFFORT
                task["effective_tier"] = (task.get("agent_tier") or "").strip() or plan.get("worker_tier") or DEFAULT_WORKER_TIER
                task["contract"] = safe_json(task.get("contract_json"), {})
            orch_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 24", (orchestrator_log_id(pid),))
            orchestrator = {
                "id": orchestrator_log_id(pid),
                "status": "running" if plan.get("status") in ("planning", "preflight", "running") else plan.get("status"),
                "model": plan.get("orchestrator_used") or plan.get("orchestrator_model"),
                "reasoning_effort": plan.get("orchestrator_effort"),
                "service_tier": plan.get("orchestrator_tier"),
                "recent_logs": list(reversed(orch_logs)),
            }
            doctor_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 20", (doctor_log_id(pid),))
            doctor = {
                "id": doctor_log_id(pid),
                "status": plan.get("preflight_status") or "idle",
                "report": safe_json(plan.get("preflight_json"), {}),
                "recent_logs": list(reversed(doctor_logs)),
            }
            q = quota_status()
            return self.send_json({"plan": plan, "tasks": tasks, "orchestrator": orchestrator, "doctor": doctor, "quota": q, "mission_usage": mission_usage(plan, q), "server_time": now()})
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
                    "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json,demo_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, goal, ws["repo_path"], "demo-simulator", "planning", now(), orchestrator_model, worker_model, max_parallel, str(mission_dir(pid)), "{}", orchestrator_effort, worker_effort, orchestrator_tier, worker_tier, json.dumps(RECOVERY_DEFAULTS), workspace_id, "auto", json.dumps(attachment_paths), 1),
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
                    "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid, goal, str(Path(workspace).expanduser().resolve()), "codex-chatgpt", "planning", now(),
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

            if p.startswith("/api/reconsider-plan/"):
                pid = p.split("/api/reconsider-plan/", 1)[1]
                mode = (data.get("mode") or "reconsider").strip()
                note = (data.get("note") or "").strip()
                return self.send_json(replan_mission(pid, mode=mode, user_note=note))

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
    print(f"AgentDock v0.11 running at {url}")
    print("Auth mode: Codex CLI signed in with ChatGPT (Plus supported).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
