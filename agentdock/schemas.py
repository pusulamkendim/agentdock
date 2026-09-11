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

def safe_json(text, default=None):
    try:
        return json.loads(text or "")
    except Exception:
        return {} if default is None else default

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
