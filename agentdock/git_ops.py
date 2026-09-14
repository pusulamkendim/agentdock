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
import tempfile
import threading
import time
import uuid
import webbrowser
import fnmatch
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .db import execute, log
FINGERPRINT_MAX_FILES = 2000

FINGERPRINT_MAX_HASH_BYTES = 64 * 1024 * 1024

FINGERPRINT_MAX_TOTAL_HASH_BYTES = 256 * 1024 * 1024

FINGERPRINT_SKIP_DIRS = {".git", ".agentdock", "node_modules", ".venv", "venv"}


def shell(args, cwd=None, input_text=None, check=True, timeout=None, env=None):
    p = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"exit {p.returncode}")[-12000:])
    return p

def git(cwd, *args, check=True, input_text=None):
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    return shell([exe, "-C", str(cwd), *args], input_text=input_text, check=check)

def git_read(cwd, *args, check=True, input_text=None):
    """Run an observational Git command without optional index refresh locks."""
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return shell([exe, "-C", str(cwd), *args], input_text=input_text, check=check, env=env)

def initialize_git_repository(workspace, branch="main"):
    """Initialize a local repository with an empty base commit.

    Existing workspace files are deliberately left untracked.  The empty
    commit gives AgentDock a stable HEAD from which isolated worktrees can be
    created without claiming ownership of user files.
    """
    root = Path(workspace).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Workspace directory does not exist")
    branch = str(branch or "main").strip() or "main"
    if git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise ValueError(f"Invalid default Git branch: {branch}")

    probe = git_read(root, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode != 0:
        git(root, "init", "-b", branch)
    head = git_read(root, "rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        git(
            root,
            "-c", "user.name=AgentDock",
            "-c", "user.email=agentdock@local",
            "commit", "--allow-empty", "-m", "Initialize AgentDock workspace",
        )
    return workspace_snapshot(root)

def repo_info(workspace):
    workspace = Path(workspace).expanduser().resolve()
    try:
        root = Path(git_read(workspace, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        head = git_read(root, "rev-parse", "HEAD").stdout.strip()
        rel = workspace.relative_to(root)
        dirty = bool(git_read(root, "status", "--porcelain").stdout.strip())
        branch = git_read(root, "branch", "--show-current", check=False).stdout.strip()
        return {"is_git": True, "root": str(root), "head": head, "rel": str(rel), "dirty": dirty, "branch": branch}
    except Exception:
        return {"is_git": False, "root": str(workspace), "head": "", "rel": ".", "dirty": False}

def git_status_entries(repo_root):
    out = git_read(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = []
    parts = [x for x in out.split("\0") if x]
    i = 0
    while i < len(parts):
        rec = parts[i]
        if len(rec) < 3:
            i += 1
            continue
        xy = rec[:2]
        path = rec[3:] if rec[2:3] == " " else rec[2:].lstrip()
        entries.append({"xy": xy, "path": path})
        # Rename/copy records include a second NUL path. It is never auto-cleaned,
        # so consume the extra field only to keep parsing aligned.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i + 1 < len(parts):
                entries[-1]["old_path"] = parts[i + 1]
                i += 1
        i += 1
    return entries

def git_remote_details(repo_root):
    """Return remote fetch/push URLs without changing Git configuration."""
    names = [x.strip() for x in git_read(repo_root, "remote", check=False).stdout.splitlines() if x.strip()]
    remotes = []
    for name in names:
        fetch = [x.strip() for x in git_read(repo_root, "config", "--get-all", f"remote.{name}.url", check=False).stdout.splitlines() if x.strip()]
        push = [x.strip() for x in git_read(repo_root, "config", "--get-all", f"remote.{name}.pushurl", check=False).stdout.splitlines() if x.strip()]
        remotes.append({"name": name, "fetch_urls": fetch, "push_urls": push or fetch})
    return remotes

def workspace_snapshot(workspace):
    """Build a bounded, deterministic, read-only snapshot for the planner."""
    resolved = Path(workspace).expanduser().resolve()
    info = repo_info(resolved)
    snapshot = {
        "workspace": str(resolved),
        "classification": "NOT_GIT",
        "repo_root": str(resolved),
        "branch": "",
        "head": "",
        "upstream": "",
        "remotes": [],
        "tracked_changes": [],
        "untracked_files": [],
        "working_tree_clean": True,
        "write_safety": {
            "read_only_inspection": "available",
            "isolated_write": "unavailable",
            "requires_user_resolution": False,
        },
        "captured_at": config.now(),
    }
    if not info.get("is_git"):
        snapshot["write_safety"].update({
            "isolated_write": "auto_initializable",
            "requires_user_resolution": False,
            "reason": "AgentDock can initialize local Git metadata with an empty base, then snapshot visible files without staging them.",
        })
        return snapshot

    root = Path(info["root"])
    entries = git_status_entries(root)
    tracked = [f"{e['xy']} {e['path']}" for e in entries if e["xy"] != "??"]
    untracked = [e["path"] for e in entries if e["xy"] == "??"]
    upstream = git_read(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).stdout.strip()
    remotes = git_remote_details(root)
    snapshot.update({
        "classification": "GIT_WITH_REMOTE" if remotes else "LOCAL_GIT",
        "repo_root": str(root),
        "branch": info.get("branch") or "",
        "head": info.get("head") or "",
        "workspace_rel": info.get("rel") or ".",
        "upstream": upstream,
        "remotes": remotes,
        "tracked_changes": tracked[:100],
        "untracked_files": untracked[:100],
        "working_tree_clean": not entries,
        "write_safety": {
            "read_only_inspection": "available",
            "isolated_write": "available",
            "requires_user_resolution": False,
            "reason": "Existing changes will be captured through a temporary index without modifying the user's index." if entries else "Clean base is available for isolated writes.",
        },
    })
    return snapshot

def _is_agentdock_state_path(path):
    target = Path(path).expanduser().resolve()
    for base in (config.STATE_ROOT, config.MISSION_ROOT, config.WORKTREE_ROOT, config.ATTACHMENT_ROOT):
        try:
            target.relative_to(Path(base).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False

def _sha256_file(path, max_bytes=None):
    digest = hashlib.sha256()
    remaining = max_bytes
    try:
        with Path(path).open("rb") as handle:
            while True:
                size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
                if size <= 0:
                    break
                chunk = handle.read(size)
                if not chunk:
                    break
                digest.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return ""

def _path_metadata(root, path, hash_budget):
    rel = str(Path(path).relative_to(root)).replace(os.sep, "/")
    try:
        stat = Path(path).lstat()
    except OSError:
        return {"path": rel, "missing": True}
    item = {
        "path": rel,
        "type": "symlink" if Path(path).is_symlink() else ("directory" if Path(path).is_dir() else "file"),
        "size": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }
    if item["type"] == "file" and item["size"] <= FINGERPRINT_MAX_HASH_BYTES and hash_budget[0] + item["size"] <= FINGERPRINT_MAX_TOTAL_HASH_BYTES:
        item["sha256"] = _sha256_file(path)
        hash_budget[0] += item["size"]
    elif item["type"] == "file":
        item["sha256"] = ""
        item["sha256_skipped"] = True
    return item

def _git_patch_hash(repo_root, *args):
    result = git_read(repo_root, "diff", *args, check=False)
    return hashlib.sha256((result.stdout or "").encode("utf-8", errors="replace")).hexdigest()


def _is_safe_generated_rel_path(path):
    """Identify disposable tool/OS artifacts that are not user work."""
    rel = Path(str(path or ""))
    if any(part in config.SAFE_GENERATED_DIRS for part in rel.parts):
        return True
    name = rel.name
    return (
        name in config.SAFE_GENERATED_FILES
        or name.endswith((".pyc", ".pyo"))
        or name.startswith(".coverage.")
    )


def workspace_fingerprint(workspace):
    """Capture the workspace state before/after a read worker without requiring a clean tree."""
    resolved = Path(workspace).expanduser().resolve()
    info = repo_info(resolved)
    fingerprint = {
        "workspace": str(resolved),
        "kind": "git" if info.get("is_git") else "filesystem",
        "captured_at": config.now(),
    }
    budget = [0]
    if info.get("is_git"):
        root = Path(info["root"])
        entries = [
            entry for entry in git_status_entries(root)
            if not _is_agentdock_state_path(root / entry.get("path", ""))
            and not _is_safe_generated_rel_path(entry.get("path", ""))
        ]
        manifest = []
        for entry in entries:
            if entry.get("xy") != "??":
                continue
            target = root / entry["path"]
            if target.exists() or target.is_symlink():
                manifest.append(_path_metadata(root, target, budget))
            else:
                manifest.append({"path": entry["path"], "missing": True})
        manifest.sort(key=lambda x: x.get("path", ""))
        fingerprint.update({
            "root": str(root),
            "head": info.get("head") or "",
            "branch": info.get("branch") or "",
            "staged_diff_hash": _git_patch_hash(root, "--cached", "--binary"),
            "unstaged_diff_hash": _git_patch_hash(root, "--binary"),
            "status_entries": sorted(
                [{k: v for k, v in entry.items() if k in ("xy", "path", "old_path")} for entry in entries],
                key=lambda x: (x.get("path", ""), x.get("xy", "")),
            ),
            "untracked_manifest": manifest[:FINGERPRINT_MAX_FILES],
        })
        return fingerprint

    manifest = []
    if resolved.is_dir():
        for current, dirs, files in os.walk(resolved, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs
                if d not in FINGERPRINT_SKIP_DIRS
                and d not in config.SAFE_GENERATED_DIRS
                and not _is_agentdock_state_path(Path(current) / d)
            )
            files = sorted(files)
            for name in files:
                if len(manifest) >= FINGERPRINT_MAX_FILES:
                    break
                path = Path(current) / name
                rel_path = path.relative_to(resolved)
                if (
                    (path.is_symlink() or path.is_file())
                    and not _is_agentdock_state_path(path)
                    and not _is_safe_generated_rel_path(rel_path)
                ):
                    manifest.append(_path_metadata(resolved, path, budget))
            if len(manifest) >= FINGERPRINT_MAX_FILES:
                break
    fingerprint["manifest"] = sorted(manifest, key=lambda x: x.get("path", ""))
    return fingerprint

def _comparable_fingerprint(value):
    if not isinstance(value, dict):
        return value
    return {key: val for key, val in value.items() if key != "captured_at"}

def fingerprint_diff(before, after):
    """Return human-readable changed paths/fields between two fingerprints."""
    before = _comparable_fingerprint(before or {})
    after = _comparable_fingerprint(after or {})
    if before == after:
        return []
    changes = []
    for key in ("kind", "root", "head", "branch", "staged_diff_hash", "unstaged_diff_hash", "status_entries"):
        if before.get(key) != after.get(key):
            changes.append(key)
    before_manifest = {x.get("path"): x for x in (before.get("untracked_manifest") or before.get("manifest") or []) if isinstance(x, dict)}
    after_manifest = {x.get("path"): x for x in (after.get("untracked_manifest") or after.get("manifest") or []) if isinstance(x, dict)}
    for path in sorted(set(before_manifest) | set(after_manifest)):
        if before_manifest.get(path) != after_manifest.get(path):
            changes.append(path or "<unknown path>")
    if not changes:
        changes.append("workspace fingerprint")
    return changes[:100]

def safe_generated_target(repo_root, rel_path):
    root = Path(repo_root).resolve()
    rel = Path(rel_path)
    parts = rel.parts
    for idx, part in enumerate(parts):
        if part in config.SAFE_GENERATED_DIRS:
            return (root / Path(*parts[: idx + 1])).resolve()
    if _is_safe_generated_rel_path(rel):
        return (root / rel).resolve()
    return None

def _safe_remove_path(repo_root, target):
    root = Path(repo_root).resolve()
    target = Path(target).resolve()
    if target == root or root not in target.parents:
        raise RuntimeError(f"Refusing cleanup outside repository: {target}")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()

def repair_local_ignore_rules(repo_root):
    common = _git_dir(repo_root, common=True)
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(errors="replace") if exclude.exists() else ""
    current = {line.strip() for line in existing.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    missing = [x for x in config.LOCAL_IGNORE_RULES if x not in current]
    if missing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a") as f:
            f.write(prefix + "\n# AgentDock self-healing generated-file rules\n")
            for rule in missing:
                f.write(rule + "\n")
    return missing

def _path_is_open(path):
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    r = shell([lsof, str(path)], check=False, timeout=3)
    return r.returncode == 0 and bool(r.stdout.strip())

def repair_known_git_locks(repo_root):
    gitdir = _git_dir(repo_root)
    repaired, blocked = [], []
    for name in ("index.lock", "HEAD.lock", "config.lock"):
        lock = gitdir / name
        if not lock.exists():
            continue
        age = max(0, time.time() - lock.stat().st_mtime)
        opened = _path_is_open(lock)
        # Only remove a lock when it is clearly stale and lsof proves no process owns it.
        if age >= 120 and opened is False:
            lock.unlink()
            repaired.append(str(lock))
        else:
            reason = "active" if opened else ("ownership unknown" if opened is None else "too recent")
            blocked.append(f"Git lock cannot be safely removed ({reason}): {lock}")
    return repaired, blocked

def sanitize_branch_component(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:40] or "task"

def plan_paths(plan_id):
    base = config.WORKTREE_ROOT / plan_id
    return base, base / "integration"

def remove_worktree(repo_root, path):
    path = Path(path)
    if path.exists():
        git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

def delete_branch(repo_root, branch):
    git(repo_root, "branch", "-D", branch, check=False)

def prepare_integration(plan):
    info = repo_info(plan["workspace"])
    if not info["is_git"]:
        raise RuntimeError("Paralel write agent'ları için workspace bir Git repository içinde olmalı.")
    repo_root = Path(info["root"])
    source_head = info["head"]
    baseline_fingerprint = workspace_fingerprint(plan["workspace"])
    base_commit = source_head
    if info["dirty"]:
        # Capture the exact visible workspace in a temporary index and an
        # unreferenced commit object. This neither changes the user's index nor
        # stages/commits their files, but gives every isolated worker the same
        # complete baseline, including current tracked and untracked work.
        base_commit = create_workspace_snapshot_commit(repo_root, source_head)
        log(
            f"orchestrator:{plan['id']}",
            "supervisor",
            "captured the current working tree as an isolated execution baseline; user files and index were not changed",
        )
    base_dir, integration_dir = plan_paths(plan["id"])
    base_dir.mkdir(parents=True, exist_ok=True)
    branch = f"agentdock/{plan['id']}/integration"
    remove_worktree(repo_root, integration_dir)
    delete_branch(repo_root, branch)
    git(repo_root, "worktree", "add", "-b", branch, str(integration_dir), base_commit)
    work_rel = Path(info["rel"])
    integration_workspace = (integration_dir / work_rel).resolve()
    execute(
        "UPDATE plans SET base_commit=?, integration_workspace=?, workspace_snapshot_json=?, error=? WHERE id=?",
        (
            base_commit,
            str(integration_workspace),
            json.dumps({
                **workspace_snapshot(plan["workspace"]),
                "execution_source_head": source_head,
                "execution_base_commit": base_commit,
                "execution_fingerprint": baseline_fingerprint,
            }, ensure_ascii=False),
            "",
            plan["id"],
        ),
    )
    return {
        "repo_root": repo_root,
        "base_commit": base_commit,
        "source_head": source_head,
        "baseline_fingerprint": baseline_fingerprint,
        "workspace_rel": work_rel,
        "integration_dir": integration_dir,
        "integration_workspace": integration_workspace,
        "integration_branch": branch,
    }


def create_workspace_snapshot_commit(repo_root, source_head):
    """Create an isolated commit object from the visible worktree.

    A temporary index is used so the user's real index, branch and files are
    untouched. Ignored files remain ignored; tracked changes and ordinary
    untracked files are represented in the resulting tree.
    """
    root = Path(repo_root).expanduser().resolve()
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    fd, index_name = tempfile.mkstemp(prefix="agentdock-index-", dir=str(config.STATE_ROOT))
    os.close(fd)
    Path(index_name).unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_name
    env.update({
        "GIT_AUTHOR_NAME": "AgentDock",
        "GIT_AUTHOR_EMAIL": "agentdock@local",
        "GIT_COMMITTER_NAME": "AgentDock",
        "GIT_COMMITTER_EMAIL": "agentdock@local",
    })
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git PATH içinde bulunamadı")
    try:
        shell([exe, "-C", str(root), "read-tree", source_head], env=env)
        shell([exe, "-C", str(root), "add", "-A", "--", "."], env=env)
        tree = shell([exe, "-C", str(root), "write-tree"], env=env).stdout.strip()
        return shell(
            [exe, "-C", str(root), "commit-tree", tree, "-p", source_head, "-m", "AgentDock execution snapshot"],
            env=env,
        ).stdout.strip()
    finally:
        Path(index_name).unlink(missing_ok=True)

def create_worker_worktree(ctx, plan, task, base_commit):
    base_dir, _ = plan_paths(plan["id"])
    existing_workspace = Path(str(task.get("workspace") or "")).expanduser()
    existing_branch = str(task.get("branch") or "").strip()
    if existing_workspace.is_dir() and existing_branch:
        # A paused worker owns this worktree. Reusing it preserves uncommitted
        # progress and, more importantly, lets the same Codex thread continue
        # without silently rebuilding the task from HEAD.
        expected_root = (base_dir / f"task-{task['seq']+1}-{task['id']}").resolve()
        detected = git_read(existing_workspace, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        worktree_root = Path(detected).expanduser().resolve() if detected else existing_workspace.resolve()
        try:
            existing_workspace.resolve().relative_to(worktree_root)
        except ValueError:
            worktree_root = existing_workspace.resolve()
        # Only reuse a worktree that belongs to this mission/task. If an old
        # path is stale or points elsewhere, the normal isolated worktree
        # creation below is safer than deleting or mutating an unknown folder.
        if worktree_root == expected_root or (
            base_dir.resolve() in worktree_root.parents
            and worktree_root.name == expected_root.name
        ):
            return worktree_root, existing_workspace.resolve(), existing_branch
    wt = base_dir / f"task-{task['seq']+1}-{task['id']}"
    branch = f"agentdock/{plan['id']}/task-{task['seq']+1}-{sanitize_branch_component(task['title'])}-{task['id']}"
    remove_worktree(ctx["repo_root"], wt)
    delete_branch(ctx["repo_root"], branch)
    git(ctx["repo_root"], "worktree", "add", "-b", branch, str(wt), base_commit)
    task_workspace = (wt / ctx["workspace_rel"]).resolve()
    execute("UPDATE tasks SET workspace=?, branch=? WHERE id=?", (str(task_workspace), branch, task["id"]))
    return wt, task_workspace, branch

def commit_worker_changes(wt, task):
    git(wt, "add", "-A")
    changed = bool(git(wt, "status", "--porcelain").stdout.strip())
    if not changed:
        return ""
    msg = f"AgentDock task {task['seq']+1}: {task['title']}"
    git(
        wt,
        "-c", "user.name=AgentDock",
        "-c", "user.email=agentdock@local",
        "commit", "-m", msg,
    )
    return git(wt, "rev-parse", "HEAD").stdout.strip()

def changed_git_paths(repo_root):
    """Return tracked, staged and untracked paths visible in a worker worktree."""
    found = set()
    for args in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        result = git(repo_root, *args, check=False)
        found.update(x for x in result.stdout.split("\0") if x)
    return sorted(found)

OUTSIDE_WORKSPACE_PATTERN = "__agentdock_outside_workspace__"


def canonical_allowed_pattern(pattern, workspace=None):
    """Normalize planner path language before validation or enforcement.

    Planner contracts often annotate a path with human guidance, while the
    runtime matcher needs only the actual pattern.  Keep that translation in
    one place and collapse every workspace-wide glob to the same canonical
    global marker so callers can reject it for write tasks.
    """
    value = str(pattern or "").strip().replace("\\", "/")
    previous = None
    while value and value != previous:
        previous = value
        value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
        value = re.sub(r"\s+only\s+when\b.*$", "", value, flags=re.I).strip()
    # Planner output may use an absolute path rooted at the mission workspace,
    # while worker Git status always reports paths relative to its isolated
    # worktree. Translate both forms into the same language. An absolute path
    # without a trusted workspace (or outside it) is never a valid capability.
    if value.startswith("/"):
        if not workspace:
            return OUTSIDE_WORKSPACE_PATTERN
        # Resolve both sides. On macOS, for example, temporary paths may be
        # presented as /var/... while Path.resolve() reports /private/var/....
        # Comparing one resolved side with one lexical side incorrectly turns
        # a valid in-workspace capability into an outside-workspace sentinel.
        workspace_path = Path(workspace).expanduser().resolve()
        candidate_path = Path(value).expanduser().resolve()
        if candidate_path == workspace_path:
            value = "**"
        elif workspace_path in candidate_path.parents:
            value = candidate_path.relative_to(workspace_path).as_posix()
        else:
            return OUTSIDE_WORKSPACE_PATTERN
    value = value.lstrip("./")
    while value.startswith("workspace/"):
        value = value[len("workspace/"):].lstrip("./")
    value = value.rstrip("/")
    glob_free = re.sub(r"\[[^]]*\]|\{[^}]*\}|[*?]", "", value).replace("/", "")
    if not value or value in ("*", "**", "**/*") or not glob_free.strip():
        return "**"
    return value

def path_matches_allowed(rel_path, pattern, workspace=None):
    rel = str(rel_path).replace("\\", "/").lstrip("./")
    pat = canonical_allowed_pattern(pattern, workspace=workspace)
    if pat in ("**", OUTSIDE_WORKSPACE_PATTERN):
        return False
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        return rel == prefix or rel.startswith(prefix + "/")
    return rel == pat or fnmatch.fnmatchcase(rel, pat)

def validate_worker_changes(worktree, task, workspace=None):
    try:
        contract = json.loads(task.get("contract_json") or "{}")
    except Exception:
        contract = {}
    if not isinstance(contract, dict):
        contract = {}
    allowed = contract.get("allowed_paths") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    changed = changed_git_paths(worktree)
    violations = [
        path for path in changed
        if not any(path_matches_allowed(path, pattern, workspace=workspace) for pattern in allowed)
    ]
    if violations:
        raise RuntimeError(
            "Worker contract dışındaki dosyaları değiştirdi: " + ", ".join(violations[:20])
        )
    return changed

def validate_read_workspace(workspace, expected_head=None, baseline=None):
    """Validate that a read task preserved the exact pre-task workspace state."""
    if baseline is None:
        baseline = workspace_fingerprint(workspace)
        if expected_head is not None and baseline.get("head") != expected_head:
            raise RuntimeError("Read-only task workspace HEAD'i task başlamadan önce beklenen commit ile eşleşmiyor.")
    after = workspace_fingerprint(workspace)
    changes = fingerprint_diff(baseline, after)
    if changes:
        raise RuntimeError(
            "Read-only task workspace'i değiştirdi; değişen alan/dosyalar: " + ", ".join(changes[:20])
        )
    return after
def _git_dir(repo_root, common=False):
    arg = "--git-common-dir" if common else "--git-dir"
    raw = git_read(repo_root, "rev-parse", arg).stdout.strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(repo_root) / p).resolve()
    return p
