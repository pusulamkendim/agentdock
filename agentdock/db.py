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

from . import config

def db():
    con = sqlite3.connect(config.DB, check_same_thread=False, timeout=30)
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
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    config.MISSION_ROOT.mkdir(parents=True, exist_ok=True)
    config.ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
    if not config.DB.exists() and config.LEGACY_DB.exists() and config.LEGACY_DB.resolve() != config.DB.resolve():
        shutil.copy2(config.LEGACY_DB, config.DB)
        print(f"Migrated legacy AgentDock database to {config.DB}")
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
            cur.execute("INSERT INTO agents(id,name,role,engine,model,mode,created_at) VALUES(?,?,?,?,?,?,?)", (*row, config.now()))
    con.commit()
    con.close()

def recover_orphaned_runs(write_docs=None):
    """Move work interrupted by a server restart into an explicit retry state."""
    interrupted_at = config.now()
    with config.DB_LOCK:
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
        log(f"orchestrator:{plan_id}", "supervisor", message)
        if write_docs:
            write_docs(plan_id)
    return len(plan_ids)

def migrate_legacy_orchestrator_state(write_docs=None):
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
            log(f"orchestrator:{plan['id']}", "supervisor", message)
            if write_docs:
                write_docs(plan["id"])
        elif not current and not legacy and plan.get("status") not in ("done", "cancelled"):
            message = "Legacy mission: unified orchestrator session must be reconstructed explicitly."
            execute(
                "UPDATE plans SET legacy_orchestrator_status=?,orchestrator_last_error=? WHERE id=?",
                ("reconstruct_required", message, plan["id"]),
            )
            log(f"orchestrator:{plan['id']}", "supervisor", message)
            if write_docs:
                write_docs(plan["id"])


def materialize_existing_workspaces(repo_info_func=None):
    """Backfill workspace rows for legacy plans using an injected inspector."""
    con = db()
    existing = con.execute("SELECT id,workspace,workspace_id FROM plans ORDER BY created_at").fetchall()
    for plan in existing:
        repo_path = str(Path(plan["workspace"]).expanduser().resolve()) if plan["workspace"] else ""
        if not repo_path:
            continue
        workspace = con.execute("SELECT id FROM workspaces WHERE repo_path=?", (repo_path,)).fetchone()
        if workspace:
            workspace_id = workspace["id"]
        else:
            workspace_id = str(uuid.uuid4())[:8]
            name = Path(repo_path).name or repo_path
            info = repo_info_func(repo_path) if repo_info_func and Path(repo_path).exists() else {"branch": ""}
            con.execute(
                "INSERT INTO workspaces(id,name,repo_path,default_branch,created_at,last_opened_at) VALUES(?,?,?,?,?,?)",
                (workspace_id, name, repo_path, info.get("branch") or "", config.now(), config.now()),
            )
        if not plan["workspace_id"]:
            con.execute("UPDATE plans SET workspace_id=? WHERE id=?", (workspace_id, plan["id"]))
    con.commit()
    con.close()

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
    with config.DB_LOCK:
        con = db()
        con.execute(sql, args)
        con.commit()
        con.close()

def log(task_id, stream, line):
    if task_id:
        execute(
            "INSERT INTO logs(task_id,ts,stream,line) VALUES(?,?,?,?)",
            (task_id, config.now(), stream, str(line)[:12000]),
        )

def claim_plan_run(plan_id):
    """Prevent duplicate starts caused by double clicks or concurrent clients."""
    with config.ACTIVE_PLAN_RUNS_LOCK:
        if plan_id in config.ACTIVE_PLAN_RUNS:
            return False
        config.ACTIVE_PLAN_RUNS.add(plan_id)
        return True

def release_plan_run(plan_id):
    with config.ACTIVE_PLAN_RUNS_LOCK:
        config.ACTIVE_PLAN_RUNS.discard(plan_id)


def plan_is_paused(plan_id):
    plan = one("SELECT status,paused FROM plans WHERE id=?", (plan_id,)) or {}
    return plan.get("status") in ("paused", "pausing") or int(plan.get("paused") or 0) == 1


def latest_orchestrator_session(plan_id):
    return one(
        """SELECT * FROM agent_sessions
           WHERE plan_id=? AND kind LIKE '%orchestrator%'
           ORDER BY CASE WHEN thread_id!='' THEN 0 ELSE 1 END, started_at DESC, rowid DESC
           LIMIT 1""",
        (plan_id,),
    )

def ensure_workspace(repo_path, name=None, repo_info_func=None):
    normalize_workspace_path = config.normalize_workspace_path
    path = normalize_workspace_path(repo_path)
    canonical = str(path)
    current = one("SELECT * FROM workspaces WHERE repo_path=?", (canonical,))
    info = repo_info_func(canonical) if repo_info_func else {"branch": ""}
    if current:
        execute("UPDATE workspaces SET last_opened_at=?, default_branch=CASE WHEN ?<>'' THEN ? ELSE default_branch END WHERE id=?",
                (config.now(), info.get("branch") or "", info.get("branch") or "", current["id"]))
        if name and name.strip() and name.strip() != current.get("name"):
            execute("UPDATE workspaces SET name=? WHERE id=?", (name.strip(), current["id"]))
        return one("SELECT * FROM workspaces WHERE id=?", (current["id"],))
    wid = str(uuid.uuid4())[:8]
    execute("INSERT INTO workspaces(id,name,repo_path,default_branch,created_at,last_opened_at) VALUES(?,?,?,?,?,?)",
            (wid, (name or path.name or canonical).strip(), canonical, info.get("branch") or "", config.now(), config.now()))
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
            (sid, plan_id or "", task_id or "", kind, model or "", effort or "", tier or "default", mode, str(cwd), "running", config.now()))
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
            (session_id, plan_id or "", task_id or "", config.now(), typ, itype, json.dumps(obj, ensure_ascii=False)[:120000]))
    if typ in ("thread.started", "thread/started"):
        thread = obj.get("thread") if isinstance(obj.get("thread"), dict) else params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = str(obj.get("thread_id") or thread.get("id") or "")
        if thread_id:
            execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (thread_id, session_id))
            # Bind the mission as soon as the first planner event exposes the
            # thread. Never replace an already-bound thread from an ordinary
            # resume; the gateway performs the hard mismatch check after the
            # turn completes.
            if plan_id and str(task_id or "") == f"orchestrator:{plan_id}":
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
            (status, config.now(), str(final_response or "")[-50000:], session_id))

def latest_agent_session(task_id):
    return one("SELECT * FROM agent_sessions WHERE task_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,))

def record_control_event(plan_id, event_type, payload=None, task_id=""):
    """Persist a human-readable control-plane event alongside raw Codex events."""
    session = latest_agent_session(f"orchestrator:{plan_id}")
    if not session:
        return
    obj = {"type": event_type, "plan_id": plan_id}
    if isinstance(payload, dict):
        obj.update(payload)
    record_codex_event(session["id"], task_id or f"orchestrator:{plan_id}", plan_id, json.dumps(obj, ensure_ascii=False))

def plan_attachment_paths(plan):
    try:
        vals = json.loads((plan or {}).get("attachments_json") or "[]")
    except Exception:
        vals = []
    if not isinstance(vals, list):
        vals = []
    if isinstance(vals, list):
        return [str(x) for x in vals if x and Path(str(x)).is_file()]
    return []

def attachment_dir(plan_id):
    path = config.ATTACHMENT_ROOT / (plan_id or "drafts")
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
            (aid, plan_id or "", task_id or "", safe_name, mime, str(path), config.now()))
    return {"id": aid, "name": safe_name, "mime": mime, "path": str(path), "size": len(raw)}
def doctor_log_id(plan_id):
    return f"doctor:{plan_id}"
