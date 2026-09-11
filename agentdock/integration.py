"""Bounded Git integration for completed write-worker results."""

import json
from pathlib import Path

from . import config
from .db import execute, log
from .git_ops import (
    changed_git_paths,
    delete_branch,
    git,
    path_matches_allowed,
    remove_worktree,
)
from .schemas import safe_json


def merge_conflict_prompt(plan, task, unresolved, cherry_error):
    contract = safe_json(task.get("contract_json"), {})
    return f"""You are the root orchestrator resolving a Git integration conflict between parallel worker results.

MISSION:
{plan['goal']}

CURRENT TASK:
TASK-{task['seq'] + 1:03d} — {task['title']}

TASK CONTRACT:
{json.dumps(contract, ensure_ascii=False, indent=2)}

CONFLICTED FILES:
{chr(10).join('- ' + path for path in unresolved) or '- unknown'}

CHERRY-PICK ERROR:
{cherry_error[-4000:]}

The integration worktree is currently in an active cherry-pick conflict state.
Resolve ONLY the conflict markers needed to preserve both already-integrated behavior and this task's explicit acceptance criteria.
You may inspect files and run focused verification. Do not broaden scope, refactor unrelated code, delete user work, change dependencies, or run git commit/cherry-pick/abort/reset commands. The harness owns Git state.
If the conflict cannot be resolved without a product/architecture decision outside the existing contracts, respond exactly with:
BLOCKED_NEEDS_USER: <reason>
Otherwise resolve the files in-place and briefly report what you reconciled.
"""


def resolve_merge_conflict(plan, ctx, result, cherry_error, orchestrator_turn=None):
    """Resolve only the files Git reported as conflicted.

    ``orchestrator_turn`` is injected by the control plane.  The compatibility
    fallback keeps direct callers working while avoiding a tasks → orchestrator
    import cycle.
    """
    task = result["task"]
    settings = config.recovery_settings(plan)
    unresolved = [
        path
        for path in git(
            ctx["integration_dir"],
            "diff",
            "--name-only",
            "--diff-filter=U",
            check=False,
        ).stdout.splitlines()
        if path.strip()
    ]
    if settings.get("merge_conflicts") != "orchestrator":
        return False, f"Merge conflict requires attention: {', '.join(unresolved) or cherry_error}"
    log(
        f"orchestrator:{plan['id']}",
        "supervisor",
        f"TASK-{task['seq'] + 1:03d} integration conflict; root orchestrator is attempting a bounded resolution",
    )
    execute("UPDATE plans SET recovery_count=recovery_count+1 WHERE id=?", (plan["id"],))
    try:
        retries = 1 if settings.get("auto_retry_transient") else 0
        if orchestrator_turn is None:
            from .orchestrator import run_mission_orchestrator_turn

            orchestrator_turn = run_mission_orchestrator_turn
        turn = orchestrator_turn(
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
        for relative_path in unresolved:
            path = Path(ctx["integration_dir"]) / relative_path
            if path.is_file():
                try:
                    content = path.read_text(errors="replace")
                    if "<<<<<<<" in content or ">>>>>>>" in content:
                        marker_files.append(relative_path)
                except Exception:
                    pass
        if marker_files:
            return False, "Orchestrator left conflict markers in: " + ", ".join(marker_files)
        changed = set(changed_git_paths(ctx["integration_dir"]))
        unresolved_set = set(unresolved)
        out_of_scope = sorted(changed - unresolved_set)
        if out_of_scope:
            return False, "Orchestrator conflict resolver changed files outside the conflicted set: " + ", ".join(out_of_scope[:20])
        contract = safe_json(task.get("contract_json"), {})
        allowed_paths = contract.get("allowed_paths") or []
        disallowed_conflicts = [
            relative_path
            for relative_path in unresolved
            if not any(path_matches_allowed(relative_path, pattern) for pattern in allowed_paths)
        ]
        if disallowed_conflicts:
            return False, "Conflict files fall outside the task contract: " + ", ".join(disallowed_conflicts[:20])
        if not unresolved:
            return False, "Git reported no conflicted files to resolve"
        # Stage only the files Git reported as unmerged.  The resolver cannot
        # smuggle unrelated changes into the integration commit.
        git(ctx["integration_dir"], "add", "--", *unresolved)
        unresolved_index = git(ctx["integration_dir"], "ls-files", "-u", check=False).stdout.strip()
        if unresolved_index:
            return False, "Orchestrator did not fully stage a conflict resolution"
        git(ctx["integration_dir"], "diff", "--cached", "--check")
        git(
            ctx["integration_dir"],
            "-c",
            "user.name=AgentDock",
            "-c",
            "user.email=agentdock@local",
            "-c",
            "core.editor=true",
            "cherry-pick",
            "--continue",
        )
        execute(
            "UPDATE tasks SET status=?, error=?, integration_status=? WHERE id=?",
            ("done", "", "resolved_by_orchestrator", task["id"]),
        )
        log(
            f"orchestrator:{plan['id']}",
            "supervisor",
            f"TASK-{task['seq'] + 1:03d} merge conflict resolved by {used}",
        )
        return True, ""
    except Exception as exc:
        return False, f"Orchestrator conflict recovery failed: {exc}"


def integrate_write_result(plan, ctx, result, orchestrator_turn=None):
    """Cherry-pick one worker result and clean up its isolated checkout."""
    task = result["task"]
    if not result.get("ok"):
        return False
    commit_hash = result.get("commit") or ""
    if not commit_hash:
        execute(
            "UPDATE tasks SET status=?, integration_status=? WHERE id=?",
            ("done", "no_changes", task["id"]),
        )
        remove_worktree(ctx["repo_root"], result["wt"])
        delete_branch(ctx["repo_root"], result["branch"])
        return True
    try:
        git(
            ctx["integration_dir"],
            "-c",
            "user.name=AgentDock",
            "-c",
            "user.email=agentdock@local",
            "cherry-pick",
            commit_hash,
        )
        execute(
            "UPDATE tasks SET status=?, integration_status=? WHERE id=?",
            ("done", "integrated", task["id"]),
        )
        remove_worktree(ctx["repo_root"], result["wt"])
        delete_branch(ctx["repo_root"], result["branch"])
        return True
    except Exception as exc:
        recovered, detail = resolve_merge_conflict(
            plan,
            ctx,
            result,
            str(exc),
            orchestrator_turn=orchestrator_turn,
        )
        if recovered:
            remove_worktree(ctx["repo_root"], result["wt"])
            delete_branch(ctx["repo_root"], result["branch"])
            return True
        git(ctx["integration_dir"], "cherry-pick", "--abort", check=False)
        error = f"Parallel integration conflict could not be self-healed: {detail or exc}"
        execute(
            "UPDATE tasks SET status=?, error=?, integration_status=?, finished_at=? WHERE id=?",
            ("failed", error, "conflict", config.now(), task["id"]),
        )
        return False


__all__ = ["integrate_write_result", "merge_conflict_prompt", "resolve_merge_conflict"]
