"""Pure data and persistence helpers for worker/orchestrator handoffs.

This module is deliberately below the task and orchestrator layers.  It knows
how to shape a handoff and record a consultation, but it does not execute a
worker or start an orchestrator turn.
"""

import json
import uuid

from . import config
from .db import execute, latest_agent_session, log, one, record_control_event
from .schemas import extract_worker_consultation, safe_json


def format_contract_md(contract):
    if not isinstance(contract, dict):
        contract = {}

    def bullets(values):
        vals = values or []
        if isinstance(vals, str):
            vals = [vals]
        return "\n".join(f"- {value}" for value in vals) or "- None specified"

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
    """Render already-completed dependency results for a worker prompt."""
    deps = json.loads(task.get("depends_json") or "[]")
    if not deps:
        return ""
    dep_rows = []
    for dependency in deps:
        row = one(
            "SELECT title,output,error,status FROM tasks WHERE plan_id=? AND seq=?",
            (task["plan_id"], dependency),
        )
        if row:
            dep_rows.append(
                f"DEPENDENCY {dependency + 1}: {row['title']}\n"
                f"STATUS: {row['status']}\n"
                f"RESULT:\n{(row['output'] or row['error'])[-6000:]}"
            )
    return "\n\n".join(dep_rows)


def create_worker_consultation(plan, task, output):
    """Persist a structured worker question without executing either side."""
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
            config.now(),
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
    log(task["id"], "supervisor", f"worker consultation queued · {request['question'][:300]}")
    log(
        f"orchestrator:{plan['id']}",
        "supervisor",
        f"TASK-{task['seq'] + 1:03d} waiting for orchestrator · consultation={consultation_id[:12]}",
    )
    record_control_event(
        plan["id"],
        "agentdock.consultation",
        {
            "consultation_id": consultation_id,
            "task_id": task["id"],
            "question": request["question"],
            "reason": request["reason"],
            "evidence": request["evidence"],
            "options": request["options"],
        },
        task_id=task["id"],
    )
    # Importing timeline at module load would create a timeline → handoffs
    # cycle.  Documentation is a side effect of recording the handoff, so a
    # local import keeps the ownership boundary explicit.
    from .timeline import write_mission_docs

    write_mission_docs(plan["id"])
    return {**request, "id": consultation_id, "worker_thread_id": worker_thread_id}


def consultation_payload(row):
    """Convert a stored consultation row into the orchestrator prompt shape."""
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


def worker_resume_message(response, contract):
    """Turn an orchestrator decision into a same-thread worker handoff."""
    return (
        "ROOT ORCHESTRATOR DECISION\n\n"
        f"{response.get('worker_message') or response.get('reason') or 'Continue the bounded task.'}\n\n"
        "Updated contract:\n"
        f"{json.dumps(contract or {}, ensure_ascii=False, indent=2)}\n\n"
        "Continue the same task from your current state. Preserve the prior context and tool findings."
    )


def same_worker_resume_handoff(
    task, instruction="Continue the same task from your last durable checkpoint."
):
    """Build durable context for resuming a worker's existing conversation."""
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


__all__ = [
    "consultation_payload",
    "create_worker_consultation",
    "format_contract_md",
    "same_worker_resume_handoff",
    "task_dependency_context",
    "worker_resume_message",
]
