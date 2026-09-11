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
