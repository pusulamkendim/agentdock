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
from .codex import run_orchestrator
from .db import (
    claim_plan_run,
    create_agent_session,
    execute,
    finish_agent_session,
    latest_agent_session,
    latest_orchestrator_session,
    log,
    one,
    plan_is_paused,
    record_control_event,
    release_plan_run,
    rows,
    save_attachment,
)
from .schemas import CONSULTATION_SCHEMA, consultation_schema_path, extract_json, extract_worker_consultation, normalize_consultation_result, safe_json
from .handoffs import (
    consultation_payload,
    create_worker_consultation,
    same_worker_resume_handoff,
    task_dependency_context,
    worker_resume_message,
)
from .timeline import write_mission_docs

def orchestrator_log_id(plan_id):
    return f"orchestrator:{plan_id}"

def _orchestrator_lock(plan_id):
    with config.ORCHESTRATOR_TURN_LOCKS_LOCK:
        lock = config.ORCHESTRATOR_TURN_LOCKS.get(plan_id)
        if lock is None:
            lock = threading.RLock()
            config.ORCHESTRATOR_TURN_LOCKS[plan_id] = lock
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
    if purpose not in config.ORCHESTRATOR_PURPOSES:
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
                json.dumps(context_payload, ensure_ascii=False), config.now(),
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
            ("running", config.now(), turn_record_id),
        )
        try:
            prompt = _orchestrator_turn_prompt(purpose, context)
            model = requested_model or plan.get("orchestrator_model") or config.DEFAULT_ORCHESTRATOR
            recovery = config.recovery_settings(plan)
            text, used_model = run_orchestrator(
                prompt,
                plan["workspace"],
                model,
                orchestrator_log_id(plan_id),
                plan.get("orchestrator_effort") or config.DEFAULT_ORCHESTRATOR_EFFORT,
                plan.get("orchestrator_tier") or config.DEFAULT_ORCHESTRATOR_TIER,
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
                 json.dumps(usage, ensure_ascii=False), config.now(), turn_record_id),
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
                (actual_thread or existing_thread, turn_id, "failed", json.dumps(usage, ensure_ascii=False), message[-12000:], config.now(), turn_record_id),
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

def _defer_consultation_for_paused_plan(plan, task, consultation, payload=None):
    """Keep a worker question durable when a mission pause interrupts resolution."""
    payload = payload or consultation_payload(consultation)
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
    payload = consultation_payload(consultation)
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
            transient_retries=1 if config.recovery_settings(plan).get("auto_retry_transient") else 0,
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
                ("resolved", config.now(), consultation["id"]),
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
            ("blocked", config.now(), consultation["id"]),
        )
        execute(
            "UPDATE tasks SET status=?,error=?,waiting_reason=? WHERE id=?",
            ("blocked", reason, "", fresh["id"]),
        )
        execute(
            "UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?",
            ("blocked", reason, config.now(), plan["id"]),
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
            transient_retries=1 if config.recovery_settings(plan).get("auto_retry_transient") else 0,
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
        transient_retries=1 if config.recovery_settings(plan).get("auto_retry_transient") else 0,
    )
    log(
        orchestrator_log_id(plan_id),
        "manual",
        "orchestrator answered the conversation message on the same mission thread",
    )


def start_orchestrator_followup(plan_id, prompt, image_paths=None):
    """Queue a manual orchestrator conversation turn in the background."""
    if not str(prompt or "").strip():
        raise ValueError("Mesaj gerekli")
    threading.Thread(
        target=run_orchestrator_followup,
        args=(plan_id, prompt, image_paths or []),
        daemon=True,
    ).start()
    return {"ok": True}

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
        "answered_at": config.now(),
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
            from .mission import run_plan
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


def answer_consultation_request(plan_id, data):
    """Parse one consultation answer and persist any attached files."""
    consultation_id = (data.get("consultation_id") or "").strip()
    if not consultation_id:
        raise ValueError("Consultation id gerekli")
    consultation = one(
        "SELECT task_id FROM consultations WHERE id=? AND plan_id=?",
        (consultation_id, plan_id),
    )
    if not consultation:
        raise ValueError("Consultation bulunamadı")
    saved = []
    for item in (data.get("attachments") or [])[:8]:
        if isinstance(item, dict) and item.get("data_base64"):
            saved.append(
                save_attachment(
                    plan_id,
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
    return answer_consultation(
        plan_id,
        consultation_id,
        answer,
        [item["path"] for item in saved],
    )

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
        transient_retries=1 if config.recovery_settings(plan).get("auto_retry_transient") else 0,
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


def start_reconstruct(plan_id):
    """Start explicit context reconstruction behind the plan run lock."""
    plan = one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Mission bulunamadı")
    if not claim_plan_run(plan_id):
        return {"ok": False, "status": "busy", "error": "Bu mission için başka bir işlem zaten çalışıyor."}
    threading.Thread(
        target=_run_reconstruct_and_release,
        args=(plan_id,),
        daemon=True,
    ).start()
    return {"ok": True, "status": "reconstructing"}
