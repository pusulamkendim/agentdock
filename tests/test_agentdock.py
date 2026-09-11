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

    def build_disposition(self, plan_id, result, workspace=None, goal="mission"):
        workspace = workspace or self.tmp
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, goal, str(workspace), "test", "planning", agentdock.now(), "gpt-5.6-sol", "gpt-5.6-luna", 2, "high", "medium", "default", "default"),
        )
        with patch.object(agentdock, "quota_status", return_value={"status": "ok", "available": True}), patch.object(
            agentdock, "run_orchestrator", return_value=(json.dumps(result), "gpt-5.6-sol")
        ):
            agentdock.build_plan(plan_id)
        return agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))


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
        self.assertEqual(payload["required"], ["decision", "reason", "evidence", "final_response", "questions", "tasks"])
        self.assertEqual(payload["properties"]["decision"]["enum"], ["already_satisfied", "answer_only", "needs_user_input", "blocked", "execute"])
        self.assertEqual(payload["properties"]["tasks"]["minItems"], 0)
        self.assertEqual(payload["properties"]["tasks"]["maxItems"], 12)
        contract = payload["properties"]["tasks"]["items"]["properties"]["contract"]
        self.assertFalse(contract["additionalProperties"])
        self.assertEqual(set(contract["required"]), set(contract["properties"]))
        self.assertFalse(contract["properties"]["scope"]["additionalProperties"])

    def test_planner_disposition_invariants_reject_invalid_task_counts(self):
        with self.assertRaisesRegex(ValueError, "requires at least one task"):
            agentdock.normalize_planner_result({
                "decision": "execute",
                "reason": "Work is required.",
                "tasks": [],
            })
        with self.assertRaisesRegex(ValueError, "must not contain tasks"):
            agentdock.normalize_planner_result({
                "decision": "already_satisfied",
                "reason": "Already done.",
                "evidence": ["Inspected the target."],
                "tasks": [{"title": "Unnecessary task"}],
            })
        with self.assertRaisesRegex(ValueError, "requires at least one question"):
            agentdock.normalize_planner_result({
                "decision": "needs_user_input",
                "reason": "A product choice is missing.",
                "tasks": [],
            })

    def test_workspace_snapshot_is_deterministic_and_read_only(self):
        repo = self.tmp / "snapshot-repo"
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
        subprocess.run(["git", "-C", str(repo), "remote", "add", "sites-origin", "https://example.invalid/site.git"], check=True)
        before = (repo / ".git" / "info" / "exclude").read_text()
        snapshot = agentdock.workspace_snapshot(repo)
        self.assertEqual(snapshot["classification"], "GIT_WITH_REMOTE")
        self.assertEqual(snapshot["branch"], "main")
        self.assertEqual(snapshot["remotes"][0]["name"], "sites-origin")
        self.assertTrue(snapshot["working_tree_clean"])
        self.assertEqual((repo / ".git" / "info" / "exclude").read_text(), before)


class MissionDispositionTests(AgentDockTestCase):
    def test_already_satisfied_creates_no_tasks(self):
        plan = self.build_disposition(
            "already",
            {"decision": "already_satisfied", "reason": "The requested flag is already present.", "evidence": ["Inspected src/app.py"], "final_response": "Nothing needs to change.", "questions": [], "tasks": []},
            goal="Add the flag",
        )
        self.assertEqual(plan["status"], "done")
        self.assertEqual(plan["decision"], "already_satisfied")
        self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", ("already",)), [])
        self.assertEqual(plan["preflight_status"], "")

    def test_answer_only_and_user_input_and_blocked_have_no_tasks(self):
        cases = [
            ("answer", "answer_only", [], "Explanation returned."),
            ("question", "needs_user_input", ["Which product behavior should win?"], ""),
            ("blocked", "blocked", [], "This requires explicit authority."),
        ]
        for plan_id, decision, questions, response in cases:
            plan = self.build_disposition(
                plan_id,
                {"decision": decision, "reason": "A focused reason.", "evidence": ["Read-only inspection completed."], "final_response": response, "questions": questions, "tasks": []},
            )
            expected_status = "done" if decision == "answer_only" else ("waiting_for_user" if decision == "needs_user_input" else "blocked")
            self.assertEqual(plan["status"], expected_status)
            self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", (plan_id,)), [])

    def test_simple_execution_uses_one_task(self):
        plan = self.build_disposition(
            "one-task",
            {"decision": "execute", "reason": "One file must change.", "evidence": ["Target file is present."], "final_response": "", "questions": [], "tasks": [{"title": "Update one file", "agent_id": "coder", "mode": "write", "depends_on": [], "contract": {"objective": "Update one file", "allowed_paths": ["src/**"]}}]},
        )
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(len(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", ("one-task",))), 1)

    def test_git_verification_can_finish_with_zero_tasks(self):
        repo = self.tmp / "git-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"], check=True, capture_output=True, text=True)
        result = {"decision": "already_satisfied", "reason": "Repository is already connected.", "evidence": ["branch main", "upstream sites-origin/main", "remote inspection"], "final_response": "No execution needed.", "questions": [], "tasks": []}
        plan = self.build_disposition("git-zero", result, workspace=repo, goal="Check whether the repository is connected")
        self.assertEqual(plan["status"], "done")
        self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", ("git-zero",)), [])

    def test_zero_task_mission_skips_execution_preflight(self):
        plan_id = self.add_plan("question-run", status="waiting_for_user")
        agentdock.execute(
            "UPDATE plans SET decision=?,decision_reason=?,questions_json=? WHERE id=?",
            ("needs_user_input", "A decision is still needed.", json.dumps(["Which scope should be used?"]), plan_id),
        )
        with patch.object(agentdock, "run_preflight", side_effect=AssertionError("zero-task mission entered preflight")):
            agentdock.run_plan(plan_id)
        self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "waiting_for_user")


class AppServerTransportTests(AgentDockTestCase):
    def test_app_server_adapter_handshakes_and_records_thread_events(self):
        plan_id = self.add_plan("app-plan", status="running")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("app-task", plan_id, 0, "App Server task", "Inspect", "pending"),
        )
        fake_codex = self.tmp / "codex-app-server"
        fake_codex.write_text(
            """#!/usr/bin/env python3
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id == 1:
        emit({"id": 1, "result": {"server": "fake"}})
    elif request_id == 2:
        emit({"id": 2, "result": {"thread": {"id": "app-thread"}}})
    elif request_id == 3:
        emit({"id": 3, "result": {"turn": {"id": "app-turn"}}})
        emit({"method": "item/completed", "params": {"item": {"id": "message-1", "type": "agentMessage", "text": "App Server result"}}})
        emit({"method": "turn/completed", "params": {"turn": {"id": "app-turn", "status": "completed"}}})
"""
        )
        fake_codex.chmod(0o755)
        real_which = agentdock.shutil.which
        with patch.object(
            agentdock.shutil,
            "which",
            side_effect=lambda name: str(fake_codex) if name == "codex" else real_which(name),
        ):
            result = agentdock.run_codex_app_server(
                "Inspect this workspace.", self.tmp, mode="read", model="gpt-5.6-luna",
                task_id="app-task", reasoning_effort="low",
            )
        self.assertEqual(result, "App Server result")
        session = agentdock.one("SELECT thread_id,status,final_response FROM agent_sessions WHERE task_id=?", ("app-task",))
        self.assertEqual(session["thread_id"], "app-thread")
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["final_response"], "App Server result")
        event_types = [x["event_type"] for x in agentdock.rows("SELECT event_type FROM agent_events WHERE task_id=? ORDER BY id", ("app-task",))]
        self.assertIn("item/completed", event_types)
        self.assertIn("turn/completed", event_types)


class ExecutionTests(AgentDockTestCase):
    def test_git_lock_warns_for_read_only_but_blocks_writes_without_deletion(self):
        repo = self.tmp / "lock-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True, capture_output=True, text=True,
        )
        lock = repo / ".git" / "index.lock"
        lock.write_text("")

        read_plan_id = self.add_plan("read-lock-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(repo), read_plan_id))
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("read-lock-task", read_plan_id, 0, "Inspect", "Inspect", "architect", "read", "[]", "pending", "{}"),
        )
        write_plan_id = self.add_plan("write-lock-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(repo), write_plan_id))
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("write-lock-task", write_plan_id, 0, "Change", "Change", "coder", "write", "[]", "pending", "{}"),
        )

        real_which = agentdock.shutil.which
        with patch.object(
            agentdock.shutil,
            "which",
            side_effect=lambda name: "codex" if name == "codex" else real_which(name),
        ):
            read_report = agentdock.run_preflight(
                agentdock.one("SELECT * FROM plans WHERE id=?", (read_plan_id,)), False
            )
            with self.assertRaises(agentdock.PreflightBlocked):
                agentdock.run_preflight(
                    agentdock.one("SELECT * FROM plans WHERE id=?", (write_plan_id,)), True
                )

        self.assertEqual(read_report["status"], "ready")
        self.assertTrue(any("read-only execution" in item for item in read_report["warnings"]))
        self.assertTrue(lock.exists())

    def test_blocked_preflight_exposes_safe_actions_without_file_selection(self):
        plan_id = self.add_plan("missing-codex", status="approved")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("missing-codex-task", plan_id, 0, "Inspect", "Inspect", "architect", "read", "[]", "pending", "{}"),
        )
        with patch.object(agentdock.shutil, "which", return_value=None):
            with self.assertRaises(agentdock.PreflightBlocked) as caught:
                agentdock.run_preflight(agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)), False)
        options = {x["id"]: x for x in caught.exception.report["action_options"]}
        self.assertIn("verify_again", options)
        self.assertIn("cancel", options)
        self.assertTrue(options["continue_read_only"]["disabled"])

    def test_read_only_continuation_keeps_write_tasks_paused(self):
        repo = self.tmp / "mixed-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True, capture_output=True, text=True,
        )
        plan_id = self.add_plan("mixed-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(repo), plan_id))
        task_values = [
            ("mixed-read", plan_id, 0, "Inspect", "Inspect", "architect", "read", "[]", "pending", "{}"),
            ("mixed-write", plan_id, 1, "Change", "Change", "coder", "write", "[]", "pending", "{}"),
        ]
        for values in task_values:
            agentdock.execute(
                "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        real_which = agentdock.shutil.which
        with patch.object(
            agentdock.shutil,
            "which",
            side_effect=lambda name: "codex" if name == "codex" else real_which(name),
        ), patch.object(agentdock, "run_codex", return_value="read result"):
            agentdock.run_plan(plan_id, read_only_only=True)
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("mixed-read",))["status"], "done")
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("mixed-write",))["status"], "pending")
        plan = agentdock.one("SELECT status,preflight_status,summary FROM plans WHERE id=?", (plan_id,))
        self.assertEqual(plan["status"], "waiting_for_user")
        self.assertEqual(plan["preflight_status"], "ready")
        self.assertIn("Write tasks remain paused", plan["summary"])

    def test_write_preflight_waits_without_mutating_user_files_or_ignore_rules(self):
        repo = self.tmp / "dirty-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"], check=True, capture_output=True, text=True)
        (repo / "user-notes.txt").write_text("keep me\n")
        exclude = repo / ".git" / "info" / "exclude"
        before = exclude.read_text()
        plan_id = self.add_plan("dirty-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(repo), plan_id))
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("dirty-task", plan_id, 0, "Write", "Write", "coder", "write", "[]", "pending", json.dumps({"objective": "Write", "allowed_paths": ["src/**"]})),
        )
        real_which = agentdock.shutil.which
        with patch.object(agentdock.shutil, "which", side_effect=lambda name: "codex" if name == "codex" else real_which(name)):
            with self.assertRaises(agentdock.PreflightWaitingForUser):
                agentdock.run_preflight(agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)), True)
        self.assertEqual(exclude.read_text(), before)
        self.assertEqual((repo / "user-notes.txt").read_text(), "keep me\n")
        self.assertEqual(agentdock.one("SELECT preflight_status FROM plans WHERE id=?", (plan_id,))["preflight_status"], "waiting_for_user")

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
