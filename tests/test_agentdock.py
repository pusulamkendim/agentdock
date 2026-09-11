import json
import shutil
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


if __name__ == "__main__":
    unittest.main()
