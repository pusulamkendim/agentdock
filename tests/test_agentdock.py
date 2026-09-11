import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentdock


class AgentDockTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentdock-test-"))
        self.original = {
            name: getattr(agentdock, name)
            for name in ("STATE_ROOT", "DB", "WORKTREE_ROOT", "MISSION_ROOT", "ATTACHMENT_ROOT", "LEGACY_DB")
        }
        agentdock.STATE_ROOT = self.tmp / "state"
        agentdock.DB = agentdock.STATE_ROOT / "agentdock.sqlite3"
        agentdock.WORKTREE_ROOT = agentdock.STATE_ROOT / "worktrees"
        agentdock.MISSION_ROOT = agentdock.STATE_ROOT / "missions"
        agentdock.ATTACHMENT_ROOT = agentdock.STATE_ROOT / "attachments"
        agentdock.LEGACY_DB = self.tmp / "missing-legacy.sqlite3"
        agentdock.init_db()

    def tearDown(self):
        for name, value in self.original.items():
            setattr(agentdock, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_plan(self, plan_id="plan-1", status="planned"):
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at) VALUES(?,?,?,?,?,?)",
            (plan_id, "test goal", str(self.tmp), "test", status, agentdock.now()),
        )
        return plan_id


class ContractAndRecoveryTests(AgentDockTestCase):
    def test_allowed_path_matching_is_conservative(self):
        self.assertTrue(agentdock.path_matches_allowed("src/app.py", "src/**"))
        self.assertTrue(agentdock.path_matches_allowed("tests/test_app.py", "tests/** only when fixtures require it"))
        self.assertTrue(agentdock.path_matches_allowed("README.md", "workspace/** (read-only)"))
        self.assertFalse(agentdock.path_matches_allowed(".env", "src/**"))

    def test_restart_moves_running_mission_to_attention(self):
        plan_id = self.add_plan(status="running")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("task-1", plan_id, 0, "task", "task", "running"),
        )
        agentdock.execute(
            "INSERT INTO agent_sessions(id,plan_id,task_id,kind,started_at) VALUES(?,?,?,?,?)",
            ("session-1", plan_id, "task-1", "worker", agentdock.now()),
        )

        self.assertEqual(agentdock.recover_orphaned_runs(), 1)
        self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "attention")
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("task-1",))["status"], "attention")
        self.assertEqual(agentdock.one("SELECT status FROM agent_sessions WHERE id=?", ("session-1",))["status"], "interrupted")

    def test_plan_lock_is_idempotent(self):
        self.assertTrue(agentdock.claim_plan_run("plan-1"))
        self.assertFalse(agentdock.claim_plan_run("plan-1"))
        agentdock.release_plan_run("plan-1")
        self.assertTrue(agentdock.claim_plan_run("plan-1"))
        agentdock.release_plan_run("plan-1")

    def test_planner_schema_is_written_and_valid_json(self):
        path = agentdock.planner_schema_path()
        payload = json.loads(path.read_text())
        self.assertEqual(payload["required"], ["tasks"])
        self.assertEqual(payload["properties"]["tasks"]["maxItems"], 12)
        contract = payload["properties"]["tasks"]["items"]["properties"]["contract"]
        self.assertFalse(contract["additionalProperties"])
        self.assertEqual(set(contract["required"]), set(contract["properties"]))
        self.assertFalse(contract["properties"]["scope"]["additionalProperties"])


class ExecutionTests(AgentDockTestCase):
    def test_read_task_can_run_in_a_non_git_workspace(self):
        plan_id = self.add_plan(status="approved")
        agentdock.execute(
            "UPDATE plans SET worker_model=?,worker_effort=?,worker_tier=? WHERE id=?",
            ("gpt-5.6-luna", "medium", "default", plan_id),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "task-read",
                plan_id,
                0,
                "Inspect files",
                "Inspect files",
                "architect",
                "read",
                "[]",
                "pending",
                json.dumps({"objective": "Inspect files", "allowed_paths": ["workspace/**"]}),
            ),
        )
        plan = agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", ("task-read",))
        ctx = {
            "repo_root": self.tmp,
            "base_commit": "",
            "workspace_rel": Path("."),
            "integration_dir": self.tmp,
            "integration_workspace": self.tmp,
            "integration_branch": "",
        }
        with patch.object(agentdock, "run_codex", return_value="read result"):
            result = agentdock.run_parallel_task(plan, task, ctx, "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "read result")

    def test_apply_plan_applies_only_the_pending_integration_diff(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True,
            capture_output=True,
            text=True,
        )
        base_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        plan_id = "plan-apply"
        integration = agentdock.WORKTREE_ROOT / plan_id / "integration"
        integration.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", f"agentdock/{plan_id}/integration", str(integration), base_commit],
            check=True,
            capture_output=True,
            text=True,
        )
        (integration / "value.txt").write_text("after\n")
        subprocess.run(["git", "-C", str(integration), "add", "value.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(integration), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "worker"],
            check=True,
            capture_output=True,
            text=True,
        )

        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,base_commit,integration_workspace,apply_status) VALUES(?,?,?,?,?,?,?,?,?)",
            (plan_id, "apply", str(repo), "test", "awaiting_apply", agentdock.now(), base_commit, str(integration), "ready"),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("task-apply", plan_id, 0, "worker", "worker", "done"),
        )

        with patch.object(agentdock, "run_preflight", return_value={}):
            result = agentdock.apply_plan(plan_id)
        self.assertTrue(result["applied"])
        self.assertEqual((repo / "value.txt").read_text(), "after\n")
        applied = agentdock.one("SELECT status,applied,apply_status FROM plans WHERE id=?", (plan_id,))
        self.assertEqual(applied["status"], "done")
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(applied["apply_status"], "applied")

    def test_full_mission_pipeline_with_fake_codex(self):
        repo = self.tmp / "mission-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True,
            capture_output=True,
            text=True,
        )
        fake_codex = self.tmp / "codex"
        fake_codex.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
prompt = args[-1] if args else ""
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if "Return ONLY valid JSON with this exact shape" in prompt:
    result = {"tasks": [
        {"title": "Inspect repository", "agent_id": "architect", "mode": "read", "depends_on": [], "contract": {"objective": "Inspect repository", "allowed_paths": ["workspace/**"]}},
        {"title": "Write focused marker", "agent_id": "coder", "mode": "write", "depends_on": [0], "contract": {"objective": "Write focused marker", "allowed_paths": ["src/**"]}},
    ]}
elif "final orchestrator synthesis" in prompt.lower():
    result = "Fake Codex synthesis completed after integration."
else:
    if "--sandbox" in args and "workspace-write" in args:
        os.makedirs("src", exist_ok=True)
        with open("src/agentdock-fake.txt", "w") as handle:
            handle.write("integrated\\n")
    result = "Fake Codex worker completed the contracted task."
print(json.dumps({"type": "item.completed", "item": {"id": "fake-message", "type": "agent_message", "text": result}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
"""
        )
        fake_codex.chmod(0o755)
        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.tmp}:{previous_path}"
        plan_id = "plan-full"
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,"
            "max_parallel,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, "fake mission", str(repo), "fake", "planning", agentdock.now(), "gpt-5.6-sol", "gpt-5.6-luna", 2, "high", "medium", "default", "default", json.dumps(agentdock.RECOVERY_DEFAULTS)),
        )
        try:
            with patch.object(agentdock, "quota_status", return_value={"status": "ok", "available": True}):
                agentdock.build_plan(plan_id)
                agentdock.execute("UPDATE plans SET status='approved',approved_at=? WHERE id=?", (agentdock.now(), plan_id))
                agentdock.run_plan(plan_id)
            self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "awaiting_apply")
            agentdock.apply_plan(plan_id)
            plan = agentdock.one("SELECT status,applied,apply_status FROM plans WHERE id=?", (plan_id,))
            self.assertEqual(plan["status"], "done")
            self.assertEqual(plan["applied"], 1)
            self.assertEqual(plan["apply_status"], "applied")
            self.assertEqual((repo / "src" / "agentdock-fake.txt").read_text(), "integrated\n")
        finally:
            os.environ["PATH"] = previous_path


if __name__ == "__main__":
    unittest.main()
