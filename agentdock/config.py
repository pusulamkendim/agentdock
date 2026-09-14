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

ROOT = Path(__file__).resolve().parent.parent
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

# A worker's Codex turn can finish before validation, commit and integration
# have finished. Manual interventions must stay queued while that durable
# execution ownership is active; the process registry alone is not enough.
TASK_INTERVENTION_QUEUE_STATUSES = frozenset({"running", "executed", "integrating"})

MANUAL_FOLLOWUP_DRAINS = set()

MANUAL_FOLLOWUP_DRAINS_LOCK = threading.RLock()

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

ORCHESTRATOR_ACTIONS = {
    "answer_worker",
    "revise_contract",
    "request_permission",
    # Legacy values remain parseable, but are normalized to a permission
    # request instead of putting the mission in a terminal blocked state.
    "ask_user",
    "block_mission",
}

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


def is_transient_error(error):
    text = str(error or "").lower()
    # Quota exhaustion and authentication failures should not be retried.
    hard = (
        "usage limit",
        "weekly limit",
        "not logged in",
        "unauthorized",
        "forbidden",
        "insufficient quota",
    )
    if any(item in text for item in hard):
        return False
    return any(re.search(pattern, text, re.I) for pattern in TRANSIENT_ERROR_PATTERNS)

MODEL_EFFORTS = {
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max"},
    "auto-best": {"low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-sol": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-terra": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-5.6-luna": {"none", "low", "medium", "high", "xhigh", "max"},
}




def now():
    return int(time.time())

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

def recovery_settings(plan):
    # config is a leaf module.  Keep this small parser local instead of
    # importing the schema/JSON helpers and creating an upward dependency.
    try:
        configured = json.loads((plan or {}).get("recovery_json") or "{}")
    except Exception:
        configured = {}
    if not isinstance(configured, dict):
        configured = {}
    out = dict(RECOVERY_DEFAULTS)
    if isinstance(configured, dict):
        out.update({k: configured[k] for k in RECOVERY_DEFAULTS if k in configured})
    return out

def mission_dir(plan_id):
    return MISSION_ROOT / plan_id
