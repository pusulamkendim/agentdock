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
from .db import execute, doctor_log_id, latest_agent_session, one, rows
from .git_ops import changed_git_paths, git, repo_info
from .schemas import safe_json
from .handoffs import format_contract_md

def latest_log_segment(items, marker):
    """Keep only the newest logical run while preserving the full raw log."""
    start = 0
    for index, item in enumerate(items or []):
        if marker in str(item.get("line") or ""):
            start = index
    return (items or [])[start:]

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
    from .orchestrator import plan_consultations

    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        return
    task_rows = rows("SELECT t.*,a.name agent_name,a.model agent_model,a.reasoning_effort agent_effort,a.service_tier agent_tier FROM tasks t LEFT JOIN agents a ON a.id=t.agent_id WHERE t.plan_id=? ORDER BY t.seq", (plan_id,))
    d = config.mission_dir(plan_id)
    td = d / "tasks"
    td.mkdir(parents=True, exist_ok=True)
    if plan.get("mission_dir") != str(d):
        execute("UPDATE plans SET mission_dir=? WHERE id=?", (str(d), plan_id))
    usage = mission_usage(plan)
    recovery = config.recovery_settings(plan)
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
    (d / "PREFLIGHT.md").unlink(missing_ok=True)
    (d / "FINAL.md").write_text(f"# Final Synthesis\n\n{plan.get('summary') or 'Mission has not finished yet.'}\n")
    log_rows = rows("SELECT id,task_id,ts,stream,line FROM logs WHERE task_id IN (?,?) OR task_id IN (SELECT id FROM tasks WHERE plan_id=?) ORDER BY id", (f"orchestrator:{plan_id}", doctor_log_id(plan_id), plan_id))
    with (d / "events.jsonl").open("w") as f:
        for item in log_rows:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

def task_diff(task):
    from .mission import demo_task_diff

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


def logs_payload(task_id):
    return {"logs": rows("SELECT * FROM logs WHERE task_id=? ORDER BY id", (task_id,))}


def events_payload(task_id):
    session = latest_agent_session(task_id)
    events = rows(
        "SELECT id,session_id,ts,event_type,item_type,payload_json FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 300",
        (task_id,),
    )
    events.reverse()
    for event in events:
        event["payload"] = safe_json(event.pop("payload_json", "{}"), {})
    return {"session": session, "events": events}


def messages_payload(task_id):
    return {"messages": rows("SELECT * FROM task_messages WHERE task_id=? ORDER BY ts,id", (task_id,))}


def diff_payload(task_id):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise KeyError("task not found")
    return {"diff": task_diff(task)}


def task_files_payload(task_id):
    """List previewable files from a task checkpoint without trusting UI events."""
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise KeyError("task not found")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],)) or {}
    paths = set()
    task_workspace = Path(task.get("workspace") or "").expanduser()
    if task_workspace.is_dir():
        try:
            paths.update(changed_git_paths(task_workspace))
        except Exception:
            pass
    commit_hash = str(task.get("commit_hash") or "").strip()
    info = repo_info(plan.get("workspace") or "")
    if commit_hash and info.get("is_git"):
        result = git(
            info["root"], "show", "--format=", "--name-only", commit_hash,
            check=False,
        )
        paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return {
        "files": [
            {
                "path": path,
                "preview_url": f"/preview.html?plan={task['plan_id']}&task={task_id}&path={path}",
            }
            for path in sorted(paths)
        ]
    }


def _preview_roots(plan, task=None):
    roots = []
    for raw in (
        (task or {}).get("workspace"),
        plan.get("integration_workspace"),
        plan.get("workspace"),
    ):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def file_preview_payload(plan_id, relative_path, task_id=""):
    """Read one mission-owned file for the in-app preview page."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise KeyError("plan not found")
    task = None
    if task_id:
        task = one("SELECT * FROM tasks WHERE id=? AND plan_id=?", (task_id, plan_id))
        if not task:
            raise KeyError("task not found")
    raw = str(relative_path or "").strip()
    if not raw:
        raise ValueError("file path is required")
    roots = _preview_roots(plan, task)
    target = Path(raw).expanduser()
    candidates = [target.resolve()] if target.is_absolute() else [(root / target).resolve() for root in roots]
    selected = None
    selected_root = None
    for candidate in candidates:
        for root in roots:
            if candidate != root and root in candidate.parents and candidate.is_file():
                selected, selected_root = candidate, root
                break
        if selected:
            break
    if not selected:
        raise KeyError("file is unavailable in this mission checkpoint")
    size = selected.stat().st_size
    if size > 8 * 1024 * 1024:
        raise ValueError("file is too large to preview")
    mime = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
    data = selected.read_bytes()
    is_text = mime.startswith("text/") or selected.suffix.lower() in {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm",
        ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt",
        ".sh", ".zsh", ".sql", ".xml", ".svg",
    }
    relative = selected.relative_to(selected_root).as_posix()
    payload = {"path": relative, "name": selected.name, "mime": mime, "size": size}
    if is_text:
        payload.update({"kind": "text", "content": data.decode("utf-8", errors="replace")})
    else:
        payload.update({"kind": "binary", "content_base64": base64.b64encode(data).decode("ascii")})
    return payload
