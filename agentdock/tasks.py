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
from .codex import run_codex, steer_app_server, task_input_attachment_paths, terminate_process
from .db import (
    claim_task_integration,
    create_agent_session,
    execute,
    finish_agent_session,
    latest_agent_session,
    log,
    one,
    rows,
    save_attachment,
)
from .git_ops import (
    commit_worker_changes,
    create_worker_worktree,
)
from .config import is_transient_error
from .handoffs import create_worker_consultation, safe_json, task_dependency_context
from .platform import open_terminal_at
from .timeline import write_mission_docs


def run_single_task(task_id):
    """Compatibility service for the legacy direct-task endpoint."""
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    workspace = task.get("workspace") or plan["workspace"]
    result = run_task_once(plan, task, workspace, force_mode=task["mode"])
    if result["ok"]:
        execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))
        drain_queued_manual_followups(task_id)


def run_manual_followup_message(message_id):
    """Deliver one persisted user message without changing task execution state."""
    message = one("SELECT * FROM task_messages WHERE id=?", (message_id,))
    if not message:
        return
    task = one("SELECT status FROM tasks WHERE id=?", (message["task_id"],))
    if task and task.get("status") in config.TASK_INTERVENTION_QUEUE_STATUSES:
        # A task may enter the scheduler-owned integration window after the
        # API persisted a standalone follow-up but before its delivery thread
        # starts. Put it back in the durable queue; never open a second turn
        # on the worker checkout.
        execute(
            "UPDATE task_messages SET status=?,error=? WHERE id=?",
            ("queued", "", message_id),
        )
        log(message["task_id"], "manual", "follow-up deferred until task integration ownership ends")
        return
    execute("UPDATE task_messages SET status=? WHERE id=?", ("sending", message_id))
    try:
        run_manual_followup(
            message["task_id"],
            message.get("text") or "Continue.",
            safe_json(message.get("attachments_json"), []),
        )
        execute(
            "UPDATE task_messages SET status=?,error=? WHERE id=?",
            ("delivered", "", message_id),
        )
    except Exception as exc:
        execute(
            "UPDATE task_messages SET status=?,error=? WHERE id=?",
            ("failed", str(exc), message_id),
        )
    # A second message can arrive while this standalone manual turn is
    # running. Once the runner has released the checkout, hand the durable
    # queue to the single drain consumer instead of leaving it stranded.
    drain_queued_manual_followups(message["task_id"])


def manual_followup_delivery_status(task_id):
    """Choose delivery from durable task ownership and live controls.

    A finished Codex process does not mean a write task is finished: contract
    validation, commit and integration still own its checkout.  Those states
    must queue interventions even when the process registries are empty.  An
    active App Server turn is the one deliberate exception for a task still in
    ``running``: its existing turn can safely accept a live steer.
    """
    task = one("SELECT status FROM tasks WHERE id=?", (task_id,))
    with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
        if task_id in config.MANUAL_FOLLOWUP_DRAINS:
            return "queued"
    with config.APP_SERVER_CONTROLS_LOCK:
        app_server_active = task_id in config.APP_SERVER_CONTROLS
    task_status = task.get("status") if task else None
    if task_status == "running" and app_server_active:
        return "sending"
    if task and task_status in config.TASK_INTERVENTION_QUEUE_STATUSES:
        return "queued"
    if task:
        # Every standalone conversation goes through the durable per-task
        # consumer.  The consumer starts immediately for terminal/paused
        # tasks, but the queue is what serializes concurrent callers.
        return "queued"
    if app_server_active:
        # Preserve the legacy control-registry behavior for a control that has
        # no corresponding task row (used by recovery/compatibility callers).
        return "sending"
    with config.RUNNERS_LOCK:
        if config.RUNNERS.get(task_id):
            return "queued"
    # Preserve the legacy fallback for callers that have a live control or
    # runner but no persisted task row.
    return "sending"


def send_task_followup(task_id, prompt, attachments=None):
    """Persist and deliver a conversational worker follow-up."""
    prompt = (prompt or "").strip()
    attachments = attachments or []
    if not prompt and not attachments:
        raise ValueError("Mesaj veya attachment gerekli")
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    image_paths = []
    for item in attachments[:8]:
        if isinstance(item, dict) and item.get("data_base64"):
            saved = save_attachment(
                task["plan_id"],
                item.get("name") or "image.png",
                item.get("mime") or "image/png",
                item["data_base64"],
                task_id=task_id,
            )
            image_paths.append(saved["path"])
    message_id = str(uuid.uuid4())[:10]
    # The task read, durable ownership decision and message insert share the
    # DB lock.  Therefore the scheduler cannot claim integration between the
    # state check and the queued-message write.
    with config.DB_LOCK:
        task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("Task bulunamadı")
        status = manual_followup_delivery_status(task_id)
        with config.APP_SERVER_CONTROLS_LOCK:
            app_server_active = (
                status == "sending"
                and task.get("status") == "running"
                and task_id in config.APP_SERVER_CONTROLS
            )
        execute(
            "INSERT INTO task_messages(id,task_id,plan_id,ts,text,attachments_json,status) VALUES(?,?,?,?,?,?,?)",
            (
                message_id,
                task_id,
                task["plan_id"],
                config.now(),
                prompt,
                json.dumps(image_paths),
                status,
            ),
        )
    if app_server_active:
        # The transport records a race as a visible failed message if the
        # active turn finishes between the check and the steer request.
        steered = steer_app_server(task_id, message_id, prompt, image_paths)
        if not steered:
            current_message = one("SELECT status FROM task_messages WHERE id=?", (message_id,)) or {}
            if current_message.get("status") == "sending":
                execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("queued", "", message_id))
                drain_queued_manual_followups(task_id)
    else:
        drain_queued_manual_followups(task_id)
        log(task_id, "manual", "user message queued for the next standalone Codex turn")
    return {"ok": True, "queued": status == "queued", "message_id": message_id}


def drain_queued_manual_followups(task_id):
    """Start one durable consumer for queued messages after ownership ends."""
    with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
        task = one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task or task.get("status") in config.TASK_INTERVENTION_QUEUE_STATUSES:
            return False
        if not one(
            "SELECT id FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id LIMIT 1",
            (task_id,),
        ):
            return False
        if task_id in config.MANUAL_FOLLOWUP_DRAINS:
            return False
        config.MANUAL_FOLLOWUP_DRAINS.add(task_id)
    threading.Thread(
        target=_drain_queued_manual_followups,
        args=(task_id,),
        daemon=True,
    ).start()
    return True


def _drain_queued_manual_followups(task_id):
    try:
        while True:
            with config.DB_LOCK:
                task = one("SELECT status FROM tasks WHERE id=?", (task_id,))
                if not task or task.get("status") in config.TASK_INTERVENTION_QUEUE_STATUSES:
                    return
                message = one(
                    "SELECT * FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id LIMIT 1",
                    (task_id,),
                )
                if message:
                    # Claim the message while the task is known to be outside
                    # the scheduler-owned states. This prevents a follow-up
                    # from starting on a checkout that is being reclaimed.
                    execute(
                        "UPDATE task_messages SET status=?,error=? WHERE id=? AND status=?",
                        ("sending", "", message["id"], "queued"),
                    )
            if not message:
                # Re-check while holding the drain lock. A sender that raced
                # this check waits for the lock and will either be consumed by
                # this loop or start a fresh drain after the lock is released.
                with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
                    task = one("SELECT status FROM tasks WHERE id=?", (task_id,))
                    pending = one(
                        "SELECT id FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id LIMIT 1",
                        (task_id,),
                    )
                    if task and task.get("status") not in config.TASK_INTERVENTION_QUEUE_STATUSES and pending:
                        continue
                    config.MANUAL_FOLLOWUP_DRAINS.discard(task_id)
                    return
            run_manual_followup_message(message["id"])
    finally:
        with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
            config.MANUAL_FOLLOWUP_DRAINS.discard(task_id)


def configure_task(task_id, data):
    """Update a task assignment before execution begins."""
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    if plan.get("status") not in ("planned", "approved"):
        raise ValueError("Task assignment yalnızca execution başlamadan önce değiştirilebilir.")
    agent_id = (data.get("agent_id") or task.get("agent_id") or "").strip()
    if agent_id and not one("SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise ValueError("Agent profile bulunamadı")
    mode = data.get("mode") or task.get("mode") or "read"
    if mode not in ("read", "write"):
        raise ValueError("Geçersiz task mode")
    execute("UPDATE tasks SET agent_id=?,mode=? WHERE id=?", (agent_id, mode, task_id))
    if plan.get("status") == "approved":
        execute("UPDATE plans SET status=?,approved_at=NULL WHERE id=?", ("planned", plan["id"]))
    write_mission_docs(plan["id"])
    return {"ok": True}


def cancel_task(task_id):
    """Permanently cancel a worker; resumable pauses use ``pause_task``."""
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    with config.RUNNERS_LOCK:
        process = config.RUNNERS.get(task_id)
    if process:
        terminate_process(process)
        execute(
            "UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?",
            ("cancelled", "User cancelled", config.now(), task_id),
        )
        return {"ok": True}
    if plan and int(plan.get("demo_mode") or 0) and task.get("status") == "running":
        execute(
            "UPDATE tasks SET status=?,error=?,finished_at=? WHERE id=?",
            ("cancelled", "User cancelled demo agent", config.now(), task_id),
        )
        return {"ok": True}
    return {"ok": False, "message": "Task çalışmıyor"}


def open_task_terminal(task_id):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError("Task bulunamadı")
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    open_terminal_at(task.get("workspace") or plan.get("workspace"))
    return {"ok": True}


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
- Treat allowed_paths as task focus, not a permission wall. Work anywhere inside the provided mission workspace when the contract outcome requires it.
- You are one parallel worker. Do not coordinate via Git branches/commits; the harness handles isolation and integration.
- Do not make architecture, product, scope, prioritization, dependency, or destructive-operation decisions.
- Low-level implementation choices are allowed only when the contract's decision_policy permits them and all stated interfaces/invariants remain unchanged.
- If a reserved decision or material ambiguity is required, return a single JSON object in this exact shape so the supervisor can decide and resume this same conversation:
  {{"type":"needs_orchestrator","question":"...","reason":"...","evidence":["..."],"options":["..."]}}
  Do not invent product, architecture, legal, identity, contact or destructive-operation facts.
- Do not claim a verification passed unless you actually ran or inspected it.
- If the task is read-only, do not modify files.
- Finish with a concise result covering deliverables, files changed/findings, verification performed, and remaining risks.
"""

def task_model(plan, agent, task=None):
    task = task or {}
    return (task.get("model_override") or "").strip() or (agent.get("model") or agent.get("agent_model") or "").strip() or plan.get("worker_model") or config.DEFAULT_WORKER

def task_effort(plan, agent, task=None):
    task = task or {}
    return (task.get("reasoning_effort_override") or "").strip() or (agent.get("reasoning_effort") or agent.get("agent_effort") or "").strip() or plan.get("worker_effort") or config.DEFAULT_WORKER_EFFORT

def task_tier(plan, agent, task=None):
    task = task or {}
    return (task.get("service_tier_override") or "").strip() or (agent.get("service_tier") or agent.get("agent_tier") or "").strip() or plan.get("worker_tier") or config.DEFAULT_WORKER_TIER

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
        ("running", config.now(), "", str(workspace), "", task["id"]),
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
        execute("UPDATE tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?", ("executed", output, "", config.now(), task["id"]))
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
        execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), config.now(), task["id"]))
        write_mission_docs(plan["id"])
        return {"ok": False, "error": str(e)}

def run_task_with_recovery(plan, task, workspace, force_mode=None):
    settings = config.recovery_settings(plan)
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
        log(f"orchestrator:{plan['id']}", "supervisor", f"TASK-{task['seq']+1:03d} transient failure; automatic retry {attempt}/{max_retries}")
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
            execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", str(e), config.now(), task["id"]))
            return {"task": task, "ok": False, "write": True, "phase": "worktree", "error": str(e)}
        result = run_task_with_recovery(plan, task, workspace)
        if result.get("paused"):
            log(f"orchestrator:{plan['id']}", "supervisor", f"TASK-{task['seq']+1:03d} paused by user; worktree and worker thread preserved")
            return {"task": task, **result, "ok": False, "paused": True, "write": True, "phase": "worker", "wt": str(wt), "branch": branch}
        # A previous interrupted/failed integration may already have produced
        # a durable worker commit. Keep it as the checkpoint if the resumed
        # turn has no additional file changes.
        commit_hash = str(task.get("commit_hash") or "")
        if result["ok"]:
            if not claim_task_integration(task["id"]):
                current = one("SELECT status FROM tasks WHERE id=?", (task["id"],)) or {}
                if current.get("status") in ("paused_by_user", "pausing"):
                    return {"task": task, **result, "ok": False, "paused": True, "write": True, "phase": "worker", "wt": str(wt), "branch": branch}
                result = {
                    "ok": False,
                    "phase": "integration_ownership",
                    "error": "Task integration ownership could not be acquired safely.",
                }
            else:
                task = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
        if result["ok"]:
            if (one("SELECT status FROM tasks WHERE id=?", (task["id"],)) or {}).get("status") in ("paused_by_user", "pausing"):
                return {"task": task, **result, "ok": False, "paused": True, "write": True, "phase": "worker", "wt": str(wt), "branch": branch}
            try:
                commit_hash = commit_worker_changes(wt, task) or commit_hash
                execute("UPDATE tasks SET commit_hash=? WHERE id=?", (commit_hash, task["id"]))
            except Exception as e:
                result = {"ok": False, "phase": "commit", "error": f"Worker checkpoint oluşturulamadı: {e}"}
                execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("failed", result["error"], config.now(), task["id"]))
        return {"task": task, "ok": result["ok"], "write": True, "phase": "worker", "wt": str(wt), "branch": branch, "commit": commit_hash, **result}
    else:
        result = run_task_with_recovery(plan, task, ctx["integration_workspace"], force_mode="read")
        if result.get("paused"):
            return {"task": task, **result, "ok": False, "paused": True, "write": False, "phase": "worker"}
        if result.get("ok"):
            if not claim_task_integration(task["id"]):
                current = one("SELECT status FROM tasks WHERE id=?", (task["id"],)) or {}
                if current.get("status") in ("paused_by_user", "pausing"):
                    return {"task": task, **result, "ok": False, "paused": True, "write": False, "phase": "worker"}
                result = {
                    "ok": False,
                    "phase": "integration_ownership",
                    "error": "Task integration ownership could not be acquired safely.",
                }
            else:
                task = one("SELECT * FROM tasks WHERE id=?", (task["id"],)) or task
        if result.get("ok"):
            # Read workers already run through the read-only Codex transport.
            # Mission execution does not add a second workspace-fingerprint
            # gate that can misclassify unrelated OS/tool activity as failure.
            mark_read_result_done({"task": task, "ok": True})
        return {"task": task, "ok": result.get("ok", False), "write": False, "phase": "worker", **result}

def mark_read_result_done(result):
    task = result["task"]
    if result.get("ok"):
        execute(
            "UPDATE tasks SET status=?, integration_status=?, finished_at=COALESCE(finished_at,?) WHERE id=?",
            ("done", "read_complete", config.now(), task["id"]),
        )
        drain_queued_manual_followups(task["id"])
        return True
    return False

def run_demo_manual_followup(task_id, prompt, image_paths=None):
    from .mission import demo_event

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
    if task.get("status") == "done":
        # A completed execution is no longer allowed to mutate the product.
        # The conversation remains available for explanation and verification,
        # but it must not reopen write ownership after integration completed.
        mode = "read"
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
def queued_messages(task_id):
    return rows("SELECT * FROM task_messages WHERE task_id=? AND status='queued' ORDER BY ts,id", (task_id,))
