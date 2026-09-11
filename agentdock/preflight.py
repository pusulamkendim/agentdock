import concurrent.futures
import base64
import hashlib
import mimetypes
import json
import os
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
from .db import claim_plan_run, doctor_log_id, execute, log, one, record_control_event, rows
from .git_ops import (
    _git_dir,
    _path_is_open,
    git,
    git_read,
    git_status_entries,
    repo_info,
    workspace_snapshot,
)
from .schemas import safe_json
from .timeline import write_mission_docs

is_transient_error = config.is_transient_error

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
        "started_at": config.now(),
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
        dry = git_read(repo_root, "worktree", "prune", "--dry-run", check=False).stdout.strip()
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
            message = f"Git lock detected ({owner}): {lock}"
            if has_write:
                report["blockers"].append(message + "; write execution requires explicit resolution")
            else:
                report["warnings"].append(message + "; read-only execution will not remove or modify it")

        entries = git_status_entries(repo_root)
        tracked = [f"{e['xy']} {e['path']}" for e in entries if e["xy"] != "??"]
        unknown = [e["path"] for e in entries if e["xy"] == "??"]
        affected = [e["path"] for e in entries]
        report["affected_paths"] = affected[:100]
        accepted_paths = set(safe_json(plan.get("workspace_choice_json"), []))
        if has_write and affected:
            if phase == "execution" and set(affected).issubset(accepted_paths):
                report["warnings"].append("User explicitly accepted the current changed paths for isolated execution.")
                report["checks"].append("Working tree choice recorded; isolated writes will use a clean worktree base")
                log(doctor_log_id(plan_id), "system", "✓ explicit workspace choice recorded for current changes")
            else:
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

    report["finished_at"] = config.now()
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
        execute("UPDATE plans SET status=?,error=?,finished_at=? WHERE id=?", ("cancelled", "Mission cancelled by user", config.now(), plan_id))
        log(f"orchestrator:{plan_id}", "supervisor", "mission cancelled by user during preflight")
        write_mission_docs(plan_id)
        return {"ok": True, "status": "cancelled"}

    info = repo_info(plan["workspace"])
    if not info.get("is_git"):
        raise ValueError("Bu aksiyon için workspace bir Git repository olmalı")
    root = Path(info["root"])
    selected_rows = selected_preflight_paths(root, plan_id, selected) if action in {"ignore", "stage", "move"} else []
    changed = []
    accepted_paths = set(safe_json(plan.get("workspace_choice_json"), []))
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
        log(f"orchestrator:{plan_id}", "supervisor", "user explicitly added local ignore rules: " + ", ".join(rules))
    elif action == "stage":
        git(root, "add", "--", *[rel for rel, _, _ in selected_rows])
        changed = [rel for rel, _, _ in selected_rows]
        accepted_paths.update(changed)
        execute(
            "UPDATE plans SET workspace_choice_json=? WHERE id=?",
            (json.dumps(sorted(accepted_paths), ensure_ascii=False), plan_id),
        )
        log(f"orchestrator:{plan_id}", "supervisor", "user explicitly staged files (no commit created): " + ", ".join(changed))
    elif action == "move":
        safe_root = config.ATTACHMENT_ROOT / plan_id / "preflight-preserved"
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
        log(f"orchestrator:{plan_id}", "supervisor", "user explicitly moved files to AgentDock safe area: " + ", ".join(x["path"] for x in moved))
    elif action == "continue_read_only":
        tasks = rows("SELECT mode FROM tasks WHERE plan_id=?", (plan_id,))
        if not any(t.get("mode") == "read" for t in tasks):
            raise ValueError("Bu mission içinde read-only çalıştırılabilecek task yok")
        execute("UPDATE plans SET status=?,error=? WHERE id=?", ("approved", "", plan_id))
        if claim_plan_run(plan_id):
            from .mission import run_plan
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
        log(f"orchestrator:{plan_id}", "supervisor", "preflight resolved by user; resuming mission automatically")
        if claim_plan_run(plan_id):
            from .mission import run_plan
            threading.Thread(target=run_plan, args=(plan_id,), kwargs={"claimed": True}, daemon=True).start()
        return {"ok": True, "status": "approved", "resuming": True, "report": latest_report, "changed": changed}
    execute("UPDATE plans SET status=?,apply_status=?,apply_error=?,error=? WHERE id=?", ("awaiting_apply", "ready", "", "", plan_id))
    log(f"orchestrator:{plan_id}", "supervisor", "apply preflight resolved by user; diff is ready for review")
    write_mission_docs(plan_id)
    return {"ok": True, "status": "awaiting_apply", "report": latest_report, "changed": changed}
