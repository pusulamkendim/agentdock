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
