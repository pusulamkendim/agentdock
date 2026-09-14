import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agentdock
from agentdock import config, git_ops, integration, mission, orchestrator, preflight, tasks


class AgentDockTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentdock-test-"))
        self.original = {
            name: getattr(config, name)
            for name in ("STATE_ROOT", "DB", "WORKTREE_ROOT", "MISSION_ROOT", "ATTACHMENT_ROOT", "LEGACY_DB")
        }
        config.STATE_ROOT = self.tmp / "state"
        config.DB = config.STATE_ROOT / "agentdock.sqlite3"
        config.WORKTREE_ROOT = config.STATE_ROOT / "worktrees"
        config.MISSION_ROOT = config.STATE_ROOT / "missions"
        config.ATTACHMENT_ROOT = config.STATE_ROOT / "attachments"
        config.LEGACY_DB = self.tmp / "missing-legacy.sqlite3"
        agentdock.init_db()

    def tearDown(self):
        for name, value in self.original.items():
            setattr(config, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_plan(self, plan_id="plan-1", status="planned"):
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at) VALUES(?,?,?,?,?,?)",
            (plan_id, "test goal", str(self.tmp), "test", status, agentdock.now()),
        )
        return plan_id

    @staticmethod
    def planner_contract(objective, allowed_paths):
        return {
            "objective": objective,
            "context": "Use the existing project and preserve unrelated behavior.",
            "scope": {"in_scope": [objective], "out_of_scope": ["Unrelated product changes"]},
            "allowed_paths": allowed_paths,
            "required_inputs": [],
            "implementation_steps": ["Inspect the bounded target", "Complete the contracted work"],
            "acceptance_criteria": ["The requested bounded outcome is complete"],
            "verification_commands": ["Run the focused verification"],
            "expected_output": ["A concise result and verification summary"],
            "escalation_conditions": ["Escalate ambiguity instead of guessing"],
            "decision_policy": "Do not broaden scope; ask the orchestrator when a material decision is required.",
        }

    def build_disposition(self, plan_id, result, workspace=None, goal="mission"):
        workspace = workspace or self.tmp
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, goal, str(workspace), "test", "planning", agentdock.now(), "gpt-5.6-sol", "gpt-5.6-luna", 2, "high", "medium", "default", "default"),
        )
        with patch.object(mission, "quota_status", return_value={"status": "ok", "available": True}), patch.object(
            mission,
            "run_mission_orchestrator_turn",
            return_value={
                "text": json.dumps(result),
                "model": "gpt-5.6-sol",
                "thread_id": "test-orchestrator-thread",
                "turn_id": "test-orchestrator-turn",
                "turn_record_id": "test-orchestrator-turn-record",
            },
        ):
            agentdock.build_plan(plan_id)
        return agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))


class ContractAndRecoveryTests(AgentDockTestCase):
    def test_completed_mission_never_exposes_a_stale_permission_request(self):
        plan = {
            "status": "done",
            "pending_question_id": "old-question",
            "pending_question_json": json.dumps({
                "kind": "permission_request",
                "question": "May I continue?",
            }),
        }
        self.assertEqual(mission.visible_pending_question(plan), {})
        plan["status"] = "waiting_for_permission"
        self.assertEqual(
            mission.visible_pending_question(plan)["question"],
            "May I continue?",
        )

    def test_allowed_path_matching_is_conservative(self):
        self.assertTrue(agentdock.path_matches_allowed("src/app.py", "src/**"))
        self.assertTrue(agentdock.path_matches_allowed("tests/test_app.py", "tests/** only when fixtures require it"))
        self.assertFalse(agentdock.path_matches_allowed("README.md", "workspace/** (read-only)"))
        self.assertFalse(agentdock.path_matches_allowed(".env", "src/**"))

    def test_allowed_path_patterns_share_one_canonical_language(self):
        global_patterns = (
            "workspace/** (read-only)",
            "** only when necessary",
            "./**",
            "**/",
            "**/*",
        )
        for pattern in global_patterns:
            with self.subTest(pattern=pattern):
                self.assertEqual(agentdock.canonical_allowed_pattern(pattern), "**")
                self.assertFalse(agentdock.path_matches_allowed("src/app.py", pattern))
        self.assertEqual(agentdock.canonical_allowed_pattern("./src/** (generated files)"), "src/**")
        self.assertTrue(agentdock.path_matches_allowed("src/app.py", "./src/** (generated files)"))
        self.assertEqual(agentdock.canonical_allowed_pattern("_fixtures/**"), "_fixtures/**")

    def test_absolute_allowed_path_is_relative_to_mission_workspace(self):
        workspace = self.tmp / "project"
        workspace.mkdir()
        target = workspace / "docs" / "report.md"
        self.assertEqual(
            agentdock.canonical_allowed_pattern(str(target), workspace=workspace),
            "docs/report.md",
        )
        self.assertTrue(
            agentdock.path_matches_allowed(
                "docs/report.md", str(target), workspace=workspace
            )
        )
        self.assertFalse(
            agentdock.path_matches_allowed(
                "docs/report.md", str(self.tmp / "other" / "report.md"), workspace=workspace
            )
        )

    def test_worker_validation_accepts_absolute_contract_path_in_workspace(self):
        workspace = self.tmp / "project"
        workspace.mkdir()
        task = {
            "contract_json": json.dumps(
                {"allowed_paths": [str(workspace / "docs" / "report.md")]}
            ),
        }
        with patch.object(agentdock.git_ops, "changed_git_paths", return_value=["docs/report.md"]):
            self.assertEqual(
                agentdock.validate_worker_changes(self.tmp, task, workspace=workspace),
                ["docs/report.md"],
            )

    def test_write_contract_paths_are_descriptive_not_permission_boundaries(self):
        workspace = self.tmp / "project"
        workspace.mkdir()
        graph = [{
            "title": "Write report",
            "agent_id": "coder",
            "mode": "write",
            "depends_on": [],
            "contract": self.planner_contract(
                "Write report", [str(self.tmp / "outside" / "report.md")]
            ),
        }]
        self.assertTrue(agentdock.validate_task_graph(graph, {"coder"}, workspace=workspace))

    def test_runtime_rejects_annotated_global_write_pattern(self):
        task = {
            "contract_json": json.dumps({"allowed_paths": ["workspace/** (read-only)"]}),
        }
        with patch.object(agentdock.git_ops, "changed_git_paths", return_value=["README.md"]):
            with self.assertRaisesRegex(RuntimeError, "README.md"):
                agentdock.validate_worker_changes(self.tmp, task)

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
        self.assertEqual(agentdock.one("SELECT restart_recovery_pending FROM plans WHERE id=?", (plan_id,))["restart_recovery_pending"], 1)

    def test_restart_retry_preserves_completed_task_checkpoints(self):
        plan_id = self.add_plan("checkpoint-plan", status="running")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status,output) VALUES(?,?,?,?,?,?,?)",
            ("done-task", plan_id, 0, "Done", "Done", "done", "preserved result"),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("running-task", plan_id, 1, "Running", "Running", "running"),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,commit_hash) VALUES(?,?,?,?,?,?,?,?)",
            ("unintegrated-write", plan_id, 2, "Unintegrated write", "Unintegrated write", "write", "executed", "worker-commit"),
        )
        agentdock.recover_orphaned_runs()
        plan = agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        agentdock.reset_plan_for_retry(plan, preserve_completed=True)
        done = agentdock.one("SELECT status,output FROM tasks WHERE id=?", ("done-task",))
        pending = agentdock.one("SELECT status,output FROM tasks WHERE id=?", ("running-task",))
        unintegrated = agentdock.one("SELECT status,commit_hash FROM tasks WHERE id=?", ("unintegrated-write",))
        self.assertEqual(done, {"status": "done", "output": "preserved result"})
        self.assertEqual(pending, {"status": "pending", "output": ""})
        self.assertEqual(unintegrated, {"status": "pending", "commit_hash": ""})
        self.assertEqual(agentdock.one("SELECT restart_recovery_pending FROM plans WHERE id=?", (plan_id,))["restart_recovery_pending"], 0)

    def test_restart_requeues_an_interrupted_consultation(self):
        plan_id = self.add_plan("consult-restart", status="running")
        agentdock.execute(
            "INSERT INTO consultations(id,plan_id,status,question,created_at) VALUES(?,?,?,?,?)",
            ("restart-consult", plan_id, "resolving", "Which choice?", agentdock.now()),
        )
        agentdock.recover_orphaned_runs()
        consultation = agentdock.one("SELECT status,resolved_at,orchestrator_response_json FROM consultations WHERE id=?", ("restart-consult",))
        self.assertEqual(consultation["status"], "queued")
        self.assertIsNone(consultation["resolved_at"])
        self.assertEqual(consultation["orchestrator_response_json"], "{}")

    def test_plan_lock_is_idempotent(self):
        self.assertTrue(agentdock.claim_plan_run("plan-1"))
        self.assertFalse(agentdock.claim_plan_run("plan-1"))
        agentdock.release_plan_run("plan-1")
        self.assertTrue(agentdock.claim_plan_run("plan-1"))
        agentdock.release_plan_run("plan-1")

    def test_latest_log_segment_hides_previous_preflight_runs(self):
        logs = [
            {"line": "preflight started · phase=execution"},
            {"line": "old blocker"},
            {"line": "preflight started · phase=execution"},
            {"line": "current check"},
        ]
        self.assertEqual(
            [item["line"] for item in agentdock.latest_log_segment(logs, "preflight started")],
            ["preflight started · phase=execution", "current check"],
        )

    def test_planner_schema_is_written_and_valid_json(self):
        path = agentdock.planner_schema_path()
        payload = json.loads(path.read_text())
        self.assertEqual(payload["required"], ["title", "decision", "reason", "evidence", "final_response", "questions", "tasks"])
        self.assertEqual(payload["properties"]["decision"]["enum"], ["already_satisfied", "answer_only", "needs_user_input", "blocked", "execute"])
        self.assertEqual(payload["properties"]["tasks"]["minItems"], 0)
        self.assertEqual(payload["properties"]["tasks"]["maxItems"], 12)
        contract = payload["properties"]["tasks"]["items"]["properties"]["contract"]
        self.assertFalse(contract["additionalProperties"])
        self.assertEqual(set(contract["required"]), set(contract["properties"]))
        self.assertFalse(contract["properties"]["scope"]["additionalProperties"])

        def assert_strict_objects(schema):
            if not isinstance(schema, dict):
                return
            if schema.get("type") == "object":
                properties = schema.get("properties", {})
                self.assertIn("required", schema)
                self.assertEqual(set(properties), set(schema["required"]))
                self.assertFalse(schema.get("additionalProperties", True))
                for child in properties.values():
                    assert_strict_objects(child)
            assert_strict_objects(schema.get("items"))
            for child in schema.get("anyOf", ()):
                assert_strict_objects(child)

        assert_strict_objects(payload)

    def test_consultation_schema_is_strict_and_supports_null_contract(self):
        payload = json.loads(agentdock.consultation_schema_path().read_text())
        self.assertEqual(payload["required"], ["action", "reason", "worker_message", "revised_contract", "questions", "evidence"])
        self.assertFalse(payload["additionalProperties"])
        revised = payload["properties"]["revised_contract"]["anyOf"]
        self.assertEqual(
            revised[1],
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )
        self.assertEqual(revised[2], {"type": "null"})
        self.assertFalse(revised[0]["additionalProperties"])

    def test_response_schemas_do_not_use_unsupported_max_properties(self):
        schemas = (
            agentdock.schemas.PLANNER_SCHEMA,
            agentdock.schemas.CONSULTATION_SCHEMA,
            agentdock.schemas.ORCHESTRATOR_CONTROL_SCHEMA,
        )
        for schema in schemas:
            self.assertNotIn("maxProperties", json.dumps(schema))

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

    def test_planner_task_graph_rejects_unsafe_semantics_without_repairing_them(self):
        def task(agent_id="coder", mode="write", depends_on=None, contract=None):
            return {
                "title": "Bounded task",
                "agent_id": agent_id,
                "mode": mode,
                "depends_on": [] if depends_on is None else depends_on,
                "contract": self.planner_contract("Bounded task", ["src/**"]) if contract is None else contract,
            }

        cases = [
            ([task(agent_id="unknown")], "unknown agent_id"),
            ([task(depends_on=[0])], "self dependency"),
            ([task(depends_on=[1]), task()], "forward dependency"),
            ([task(depends_on=[1])], "out of range"),
            ([task(), task(depends_on=[0, 0])], "duplicate dependency"),
            ([task(mode="append")], "invalid mode"),
            ([task(contract={"objective": "Incomplete"})], "contract is incomplete"),
        ]
        for graph, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    agentdock.validate_task_graph(graph, {"architect", "coder"})

    def test_build_plan_rejects_invalid_graph_before_materializing_tasks(self):
        plan = self.build_disposition(
            "invalid-graph",
            {
                "decision": "execute",
                "reason": "Two tasks are needed.",
                "evidence": ["The requested work is not present."],
                "final_response": "",
                "questions": [],
                "tasks": [
                    {
                        "title": "Invalid dependency",
                        "agent_id": "coder",
                        "mode": "write",
                        "depends_on": [0],
                        "contract": self.planner_contract("Update the bounded file", ["src/**"]),
                    }
                ],
            },
        )
        self.assertEqual(plan["status"], "attention")
        self.assertIn("self dependency", plan["error"])
        self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", ("invalid-graph",)), [])

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

    def test_read_fingerprint_uses_the_existing_dirty_state_as_baseline(self):
        repo = self.tmp / "fingerprint-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True, capture_output=True, text=True,
        )
        (repo / "README.md").write_text("user change\n")
        (repo / "notes.txt").write_text("user file\n")
        baseline = agentdock.workspace_fingerprint(repo)
        after = agentdock.validate_read_workspace(repo, baseline=baseline)
        self.assertEqual(agentdock.fingerprint_diff(baseline, after), [])
        (repo / "notes.txt").write_text("agent changed the user file\n")
        with self.assertRaisesRegex(RuntimeError, "değişen alan/dosyalar"):
            agentdock.validate_read_workspace(repo, baseline=baseline)

    def test_safe_generated_file_does_not_invalidate_workspace_fingerprint(self):
        repo = self.tmp / "safe-generated-fingerprint-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("baseline\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True, capture_output=True, text=True,
        )

        baseline = agentdock.workspace_fingerprint(repo)
        (repo / ".DS_Store").write_bytes(b"disposable macOS metadata")
        after_generated_file = agentdock.workspace_fingerprint(repo)
        self.assertEqual(agentdock.fingerprint_diff(baseline, after_generated_file), [])

        (repo / "meaningful.txt").write_text("must be detected\n")
        after_meaningful_file = agentdock.workspace_fingerprint(repo)
        self.assertIn("meaningful.txt", agentdock.fingerprint_diff(baseline, after_meaningful_file))


class OrchestratorCoordinationTests(AgentDockTestCase):
    def _orchestrator_plan(self, plan_id="orchestrator-plan"):
        agentdock.execute(
            "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,orchestrator_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, "coordinate a task", str(self.tmp), "test", "running", agentdock.now(), "gpt-5.6-sol", "gpt-5.6-luna", 2, "high", "medium", "default", "default", ""),
        )
        return agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))

    def test_mission_gateway_reuses_one_orchestrator_thread(self):
        plan = self._orchestrator_plan()
        calls = []
        prompts = []

        def fake_orchestrator(*args, **kwargs):
            resume = kwargs.get("resume_thread_id") or ""
            calls.append(resume)
            prompts.append(args[0])
            sid = agentdock.create_agent_session(
                plan["id"], agentdock.orchestrator_log_id(plan["id"]), "orchestrator",
                "gpt-5.6-sol", "high", "default", "read", self.tmp,
            )
            thread = "mission-thread"
            agentdock.execute("UPDATE agent_sessions SET thread_id=?,turn_id=? WHERE id=?", (thread, f"turn-{len(calls)}", sid))
            return "orchestrator result", "gpt-5.6-sol"

        with patch.object(orchestrator, "run_orchestrator", side_effect=fake_orchestrator):
            first = agentdock.run_mission_orchestrator_turn(plan["id"], "initial_disposition", "initial")
            second = agentdock.run_mission_orchestrator_turn(plan["id"], "manual_message", "continue")

        self.assertEqual(calls, ["", "mission-thread"])
        self.assertEqual(first["thread_id"], second["thread_id"])
        stored = agentdock.one("SELECT orchestrator_thread_id,orchestrator_last_turn_id,orchestrator_turn_status FROM plans WHERE id=?", (plan["id"],))
        self.assertEqual(stored["orchestrator_thread_id"], "mission-thread")
        self.assertEqual(stored["orchestrator_last_turn_id"], "turn-2")
        self.assertEqual(stored["orchestrator_turn_status"], "completed")
        self.assertEqual(agentdock.one("SELECT COUNT(*) c FROM orchestrator_turns WHERE plan_id=?", (plan["id"],))["c"], 2)
        self.assertIn("MISSION CONTEXT", prompts[0])
        self.assertIn("TASK GRAPH", prompts[0])
        self.assertIn("TURN-SPECIFIC INPUT\ncontinue", prompts[1])

    def test_mission_context_builder_includes_graph_delta_consultations_and_integration(self):
        plan = self._orchestrator_plan("context-plan")
        agentdock.execute(
            "UPDATE plans SET decision=?,apply_status=?,integration_workspace=? WHERE id=?",
            ("execute", "ready", "/tmp/integration", plan["id"]),
        )
        agentdock.execute(
            """INSERT INTO orchestrator_turns(
                id,plan_id,purpose,status,context_json,created_at,finished_at
            ) VALUES(?,?,?,?,?,?,?)""",
            ("checkpoint", plan["id"], "initial_disposition", "completed", "{}", 90, 100),
        )
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,
                output,finished_at,integration_status,commit_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "new-result", plan["id"], 0, "Implement engine", "Implement", "coder",
                "write", "[]", "done", "new worker result", 101, "integrated", "abc123",
            ),
        )
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,
                output,finished_at,integration_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "old-result", plan["id"], 1, "Old research", "Research", "researcher",
                "read", "[0]", "done", "old worker result", 99, "read_complete",
            ),
        )
        agentdock.execute(
            """INSERT INTO consultations(
                id,plan_id,task_id,status,question,reason,evidence_json,options_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "consultation", plan["id"], "new-result", "waiting_for_user",
                "Which risk limit?", "No limit was supplied.", '["config missing"]',
                '["1%", "2%"]', 102,
            ),
        )

        context = agentdock.build_mission_context(plan["id"], purpose="worker_consultation")

        self.assertIn("MISSION\n", context)
        self.assertIn("TASK-001", context)
        self.assertIn("deps:1", context)
        self.assertIn("new worker result", context)
        self.assertNotIn("old worker result", context)
        self.assertIn("Which risk limit?", context)
        self.assertIn("TASK-001: integrated commit:abc123", context)
        self.assertIn("apply: ready", context)
        self.assertIn("untrusted evidence", context)

    def test_worker_consultation_resumes_the_same_worker_thread_with_handoff(self):
        plan = self._orchestrator_plan("consult-plan")
        task_id = "consult-task"
        contract = {
            "objective": "Choose the bounded contact behavior",
            "allowed_paths": ["src/**"],
            "scope": {"in_scope": ["contact behavior"], "out_of_scope": ["unrelated UI"]},
        }
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json,worker_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, plan["id"], 0, "Implement contact behavior", "Choose the bounded contact behavior", "coder", "read", "[]", "pending", json.dumps(contract), "worker-thread"),
        )
        agentdock.execute(
            "INSERT INTO agent_sessions(id,plan_id,task_id,kind,thread_id,status,started_at) VALUES(?,?,?,?,?,?,?)",
            ("worker-session", plan["id"], task_id, "worker", "worker-thread", "completed", agentdock.now()),
        )
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        output = json.dumps({
            "type": "needs_orchestrator",
            "question": "Which contact behavior is allowed?",
            "reason": "No verified contact details exist.",
            "evidence": ["No mailto or tel links found."],
            "options": ["Omit the field", "Link to the existing tool"],
        })
        consultation = agentdock.create_worker_consultation(plan, task, output)
        response = {
            "action": "answer_worker",
            "reason": "Use the existing tool link.",
            "worker_message": "Do not invent contact details; link only to the existing tool.",
            "revised_contract": None,
            "questions": [],
            "evidence": ["The existing tool is present."],
        }
        with patch.object(
            orchestrator,
            "run_mission_orchestrator_turn",
            return_value={"text": json.dumps(response), "model": "gpt-5.6-sol", "thread_id": "mission-thread", "turn_id": "turn-2"},
        ):
            resolved = agentdock.resolve_worker_consultation(plan, task, result={"waiting_for_orchestrator": True, "consultation": consultation})
        self.assertTrue(resolved["retry"])
        updated = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["worker_thread_id"], "worker-thread")
        self.assertIn("ROOT ORCHESTRATOR DECISION", updated["worker_resume_message"])
        self.assertEqual(updated["consultation_id"], "")

        captured = {}

        def fake_worker(prompt, workspace, mode, model, task_id, effort, tier, **kwargs):
            captured.update(prompt=prompt, resume=kwargs.get("resume_thread_id"))
            return "continued worker result"

        with patch.object(tasks, "run_codex", side_effect=fake_worker):
            result = agentdock.run_task_once(plan, updated, self.tmp)
        self.assertTrue(result["ok"], result)
        self.assertEqual(captured["resume"], "worker-thread")
        self.assertIn("Do not invent contact details", captured["prompt"])
        self.assertEqual(agentdock.one("SELECT worker_resume_message,status FROM tasks WHERE id=?", (task_id,))["status"], "executed")

    def test_manual_followup_preserves_canonical_execution_state(self):
        plan = self._orchestrator_plan("manual-followup-state")
        states = ("failed", "blocked", "paused_by_user", "done")
        for index, status in enumerate(states):
            task_id = f"manual-followup-{index}"
            thread_id = f"worker-thread-{index}"
            agentdock.execute(
                """INSERT INTO tasks(
                    id,plan_id,seq,title,instructions,agent_id,mode,status,output,error,
                    commit_hash,integration_status,worker_thread_id,finished_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    plan["id"],
                    index,
                    "Manual follow-up",
                    "Preserve the execution result",
                    "coder",
                    "write",
                    status,
                    f"canonical output {status}",
                    f"canonical error {status}",
                    f"commit-{index}",
                    f"integration-{status}",
                    thread_id,
                    4242 + index,
                ),
            )
            agentdock.execute(
                "INSERT INTO agent_sessions(id,plan_id,task_id,kind,thread_id,status,started_at) VALUES(?,?,?,?,?,?,?)",
                (f"manual-session-{index}", plan["id"], task_id, "worker", thread_id, "completed", agentdock.now()),
            )
            before = agentdock.one(
                """SELECT status,output,error,commit_hash,integration_status,
                          worker_thread_id,finished_at FROM tasks WHERE id=?""",
                (task_id,),
            )
            captured = {}

            def fake_codex(*args, **kwargs):
                captured.update(kwargs)
                return f"chat reply {status}"

            with patch.object(tasks, "run_codex", side_effect=fake_codex):
                result = agentdock.run_manual_followup(task_id, "Why did this task stop?")

            self.assertEqual(result, f"chat reply {status}")
            self.assertEqual(captured["resume_thread_id"], thread_id)
            self.assertEqual(captured["session_kind"], "manual")
            after = agentdock.one(
                """SELECT status,output,error,commit_hash,integration_status,
                          worker_thread_id,finished_at FROM tasks WHERE id=?""",
                (task_id,),
            )
            self.assertEqual(after, before)

    def test_manual_followup_failure_does_not_turn_task_into_attention(self):
        plan = self._orchestrator_plan("manual-followup-error")
        task_id = "manual-followup-error-task"
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,status,output,error,
                commit_hash,integration_status,worker_thread_id,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                plan["id"],
                0,
                "Failed write task",
                "Preserve the failed execution checkpoint",
                "coder",
                "write",
                "failed",
                "canonical failed output",
                "original execution error",
                "failed-commit",
                "conflict",
                "failed-worker-thread",
                5151,
            ),
        )
        agentdock.execute(
            "INSERT INTO agent_sessions(id,plan_id,task_id,kind,thread_id,status,started_at) VALUES(?,?,?,?,?,?,?)",
            ("manual-error-session", plan["id"], task_id, "worker", "failed-worker-thread", "completed", agentdock.now()),
        )
        before = agentdock.one(
            "SELECT status,output,error,commit_hash,integration_status,finished_at FROM tasks WHERE id=?",
            (task_id,),
        )

        def fail_manual_turn(*args, **kwargs):
            raise RuntimeError("manual conversation failed")

        with patch.object(tasks, "run_codex", side_effect=fail_manual_turn):
            with self.assertRaisesRegex(RuntimeError, "manual conversation failed"):
                agentdock.run_manual_followup(task_id, "Why did this task fail?")

        after = agentdock.one(
            "SELECT status,output,error,commit_hash,integration_status,finished_at FROM tasks WHERE id=?",
            (task_id,),
        )
        self.assertEqual(after, before)

    def test_demo_manual_followup_preserves_canonical_execution_state(self):
        plan = self._orchestrator_plan("demo-followup-state")
        agentdock.execute("UPDATE plans SET demo_mode=1 WHERE id=?", (plan["id"],))
        task_id = "demo-followup-task"
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,status,output,error,
                commit_hash,integration_status,worker_thread_id,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                plan["id"],
                0,
                "Completed demo task",
                "Preserve the demo execution checkpoint",
                "coder",
                "write",
                "done",
                "canonical demo output",
                "",
                "demo-commit",
                "integrated",
                "demo-worker-thread",
                6161,
            ),
        )
        before = agentdock.one(
            "SELECT status,output,error,commit_hash,integration_status,worker_thread_id,finished_at FROM tasks WHERE id=?",
            (task_id,),
        )

        with patch.object(agentdock.time, "sleep"):
            response = agentdock.run_demo_manual_followup(task_id, "Explain the completed result.")

        self.assertIn("preserving the task contract", response)
        self.assertEqual(
            agentdock.one(
                "SELECT status,output,error,commit_hash,integration_status,worker_thread_id,finished_at FROM tasks WHERE id=?",
                (task_id,),
            ),
            before,
        )
        self.assertEqual(
            agentdock.one("SELECT kind,thread_id,status FROM agent_sessions WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)),
            {"kind": "demo-manual", "thread_id": "demo-worker-thread", "status": "completed"},
        )

    def test_queued_manual_followup_does_not_replace_execution_output(self):
        plan = self._orchestrator_plan("queued-followup-state")
        task_id = "queued-followup-task"
        thread_id = "queued-worker-thread"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,status,output,worker_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                plan["id"],
                0,
                "Read task",
                "Run the contracted inspection",
                "researcher",
                "read",
                "pending",
                "",
                thread_id,
            ),
        )
        agentdock.execute(
            "INSERT INTO agent_sessions(id,plan_id,task_id,kind,thread_id,status,started_at) VALUES(?,?,?,?,?,?,?)",
            ("queued-worker-session", plan["id"], task_id, "worker", thread_id, "completed", agentdock.now()),
        )
        message_id = "queued-followup-message"
        agentdock.execute(
            "INSERT INTO task_messages(id,task_id,plan_id,ts,text,status) VALUES(?,?,?,?,?,?)",
            (message_id, task_id, plan["id"], agentdock.now(), "Please explain the result.", "queued"),
        )
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        calls = []

        def fake_codex(*args, **kwargs):
            calls.append(kwargs.get("session_kind"))
            return "canonical execution result" if len(calls) == 1 else "manual chat reply"

        with patch.object(tasks, "run_codex", side_effect=fake_codex):
            result = agentdock.run_task_once(plan, task, self.tmp)

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, ["worker", "manual"])
        self.assertEqual(result["output"], "canonical execution result")
        self.assertEqual(
            agentdock.one("SELECT status,output FROM tasks WHERE id=?", (task_id,)),
            {"status": "executed", "output": "canonical execution result"},
        )
        self.assertEqual(
            agentdock.one("SELECT status,error FROM task_messages WHERE id=?", (message_id,)),
            {"status": "delivered", "error": ""},
        )

    def test_executed_manual_followup_is_started_when_no_runner_is_active(self):
        task_id = "executed-followup-delivery"
        with config.RUNNERS_LOCK:
            old_runners = dict(config.RUNNERS)
            config.RUNNERS.clear()
        with config.APP_SERVER_CONTROLS_LOCK:
            old_controls = dict(config.APP_SERVER_CONTROLS)
            config.APP_SERVER_CONTROLS.clear()
        try:
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "sending")
            with config.RUNNERS_LOCK:
                config.RUNNERS[task_id] = object()
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS[task_id] = object()
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "sending")
        finally:
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
                config.RUNNERS.update(old_runners)
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
                config.APP_SERVER_CONTROLS.update(old_controls)

    def test_integration_owned_task_queues_followup_even_without_a_runner(self):
        plan_id = self.add_plan("integration-window-status", status="running")
        task_id = "integration-window-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Integrate safely", "Integrate safely", "write", "executed", "worker-thread"),
        )
        with config.RUNNERS_LOCK:
            old_runners = dict(config.RUNNERS)
            config.RUNNERS.clear()
        with config.APP_SERVER_CONTROLS_LOCK:
            old_controls = dict(config.APP_SERVER_CONTROLS)
            config.APP_SERVER_CONTROLS.clear()
        try:
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            agentdock.execute("UPDATE tasks SET status=? WHERE id=?", ("integrating", task_id))
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            agentdock.execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
        finally:
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
                config.RUNNERS.update(old_runners)
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
                config.APP_SERVER_CONTROLS.update(old_controls)

    def test_running_task_prefers_active_app_server_steering(self):
        plan_id = self.add_plan("live-steering-status", status="running")
        task_id = "live-steering-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Steer safely", "Steer safely", "write", "running", "worker-thread"),
        )
        with config.RUNNERS_LOCK:
            old_runners = dict(config.RUNNERS)
            config.RUNNERS.clear()
        with config.APP_SERVER_CONTROLS_LOCK:
            old_controls = dict(config.APP_SERVER_CONTROLS)
            config.APP_SERVER_CONTROLS.clear()
            config.APP_SERVER_CONTROLS[task_id] = object()
        try:
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "sending")
            with patch.object(tasks, "steer_app_server", return_value=True) as steer:
                response = tasks.send_task_followup(task_id, "Use the existing contact link.")
            self.assertFalse(response["queued"])
            steer.assert_called_once()
            self.assertEqual(
                agentdock.one("SELECT status FROM task_messages WHERE id=?", (response["message_id"],))["status"],
                "sending",
            )

            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            agentdock.execute("UPDATE tasks SET status=? WHERE id=?", ("executed", task_id))
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS[task_id] = object()
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            agentdock.execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
        finally:
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
                config.RUNNERS.update(old_runners)
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
                config.APP_SERVER_CONTROLS.update(old_controls)

    def test_standalone_followups_use_one_per_task_consumer(self):
        plan_id = self.add_plan("standalone-followup-serialization", status="running")
        task_id = "standalone-followup-serialization-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Completed task", "Explain the result", "write", "done", "worker-thread"),
        )
        with config.RUNNERS_LOCK:
            old_runners = dict(config.RUNNERS)
            config.RUNNERS.clear()
        with config.APP_SERVER_CONTROLS_LOCK:
            old_controls = dict(config.APP_SERVER_CONTROLS)
            config.APP_SERVER_CONTROLS.clear()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def deliver(message_id):
            calls.append(message_id)
            if len(calls) == 1:
                started.set()
                release.wait(2)
            agentdock.execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("delivered", "", message_id))

        try:
            with patch.object(tasks, "run_manual_followup_message", side_effect=deliver):
                first = tasks.send_task_followup(task_id, "First explanation")
                self.assertTrue(first["queued"])
                self.assertTrue(started.wait(2))
                second = tasks.send_task_followup(task_id, "Second explanation")
                self.assertTrue(second["queued"])
                self.assertEqual(calls, [first["message_id"]])
                release.set()
                deadline = time.time() + 2
                while len(calls) < 2 and time.time() < deadline:
                    time.sleep(0.01)

            self.assertEqual(calls, [first["message_id"], second["message_id"]])
            self.assertEqual(
                agentdock.one("SELECT status FROM task_messages WHERE id=?", (second["message_id"],))["status"],
                "delivered",
            )
        finally:
            release.set()
            with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
                config.MANUAL_FOLLOWUP_DRAINS.discard(task_id)
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
                config.RUNNERS.update(old_runners)
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
                config.APP_SERVER_CONTROLS.update(old_controls)

    def test_followup_queued_during_integration_drains_after_task_is_done(self):
        plan_id = self.add_plan("integration-window-drain", status="running")
        task_id = "integration-window-drain-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Integrate safely", "Integrate safely", "write", "executed", "worker-thread"),
        )
        with config.RUNNERS_LOCK:
            old_runners = dict(config.RUNNERS)
            config.RUNNERS.clear()
        with config.APP_SERVER_CONTROLS_LOCK:
            old_controls = dict(config.APP_SERVER_CONTROLS)
            config.APP_SERVER_CONTROLS.clear()
        try:
            response = tasks.send_task_followup(task_id, "Please explain the integration result.")
            self.assertTrue(response["queued"])
            message_id = response["message_id"]
            self.assertEqual(
                agentdock.one("SELECT status FROM task_messages WHERE id=?", (message_id,))["status"],
                "queued",
            )
            self.assertTrue(agentdock.claim_task_integration(task_id))
            self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"], "integrating")

            delivered = []

            def deliver(message_id):
                delivered.append(message_id)
                agentdock.execute("UPDATE task_messages SET status=? WHERE id=?", ("delivered", message_id))

            with patch.object(tasks, "run_manual_followup_message", side_effect=deliver):
                agentdock.execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))
                self.assertTrue(tasks.drain_queued_manual_followups(task_id))
                deadline = time.time() + 2
                while time.time() < deadline:
                    current = agentdock.one(
                        "SELECT status FROM task_messages WHERE id=?", (message_id,)
                    )["status"]
                    if current == "delivered":
                        break
                    time.sleep(0.01)

            self.assertEqual(delivered, [message_id])
            self.assertEqual(
                agentdock.one("SELECT status FROM task_messages WHERE id=?", (message_id,))["status"],
                "delivered",
            )
            self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"], "done")
        finally:
            with config.MANUAL_FOLLOWUP_DRAINS_LOCK:
                config.MANUAL_FOLLOWUP_DRAINS.discard(task_id)
            with config.RUNNERS_LOCK:
                config.RUNNERS.clear()
                config.RUNNERS.update(old_runners)
            with config.APP_SERVER_CONTROLS_LOCK:
                config.APP_SERVER_CONTROLS.clear()
                config.APP_SERVER_CONTROLS.update(old_controls)

    def test_followup_stays_queued_until_integration_cleanup_finishes(self):
        plan_id = self.add_plan("integration-cleanup-window", status="running")
        task_id = "integration-cleanup-window-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Integrate safely", "Integrate safely", "write", "executed", "worker-thread"),
        )
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()

        def delayed_cleanup(*args, **kwargs):
            cleanup_started.set()
            self.assertTrue(allow_cleanup.wait(2))

        result = {
            "task": agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,)),
            "ok": True,
            "commit": "",
            "wt": str(self.tmp / "unused-worker-worktree"),
            "branch": "worker",
        }
        with patch.object(integration, "remove_worktree", side_effect=delayed_cleanup), patch.object(
            integration, "delete_branch"
        ):
            worker = threading.Thread(
                target=integration.integrate_write_result,
                args=(
                    agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)),
                    {"repo_root": self.tmp, "integration_dir": self.tmp},
                    result,
                ),
            )
            worker.start()
            self.assertTrue(cleanup_started.wait(2))
            self.assertEqual(
                agentdock.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"],
                "integrating",
            )
            self.assertEqual(agentdock.manual_followup_delivery_status(task_id), "queued")
            allow_cleanup.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

        self.assertEqual(
            agentdock.one("SELECT status,integration_status FROM tasks WHERE id=?", (task_id,)),
            {"status": "done", "integration_status": "no_changes"},
        )

    def test_done_manual_followup_forces_read_only_mode(self):
        plan = self._orchestrator_plan("done-followup-read-only")
        task_id = "done-followup-read-only-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,status,worker_thread_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, plan["id"], 0, "Completed write", "Explain the result", "coder", "write", "done", "done-thread"),
        )
        captured = {}

        def fake_codex(*args, **kwargs):
            captured["mode"] = args[2]
            return "read-only explanation"

        with patch.object(tasks, "run_codex", side_effect=fake_codex):
            result = agentdock.run_manual_followup(task_id, "Explain the completed result.")

        self.assertEqual(result, "read-only explanation")
        self.assertEqual(captured["mode"], "read")

    def test_pausing_consultation_stays_queued_after_orchestrator_interrupt(self):
        plan = self._orchestrator_plan("paused-consultation")
        task_id = "paused-consultation-task"
        consultation_id = "paused-consultation-record"
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,status,waiting_reason,
                consultation_id,worker_thread_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                plan["id"],
                0,
                "Needs orchestrator decision",
                "Wait for the orchestrator",
                "coder",
                "write",
                "waiting_for_orchestrator",
                "Which contact behavior is allowed?",
                consultation_id,
                "worker-consult-thread",
            ),
        )
        agentdock.execute(
            """INSERT INTO consultations(
                id,plan_id,task_id,status,question,reason,evidence_json,options_json,
                worker_thread_id,orchestrator_response_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                consultation_id,
                plan["id"],
                task_id,
                "queued",
                "Which contact behavior is allowed?",
                "No verified contact details exist.",
                json.dumps(["No mailto or tel link was found"]),
                json.dumps(["Omit the field", "Use the existing tool"]),
                "worker-consult-thread",
                "{}",
                agentdock.now(),
            ),
        )
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))

        def interrupt_consultation(*args, **kwargs):
            agentdock.execute(
                "UPDATE plans SET status=?,paused=1,pause_reason=? WHERE id=?",
                ("pausing", "Paused by user", plan["id"]),
            )
            raise RuntimeError("orchestrator turn interrupted by pause")

        with patch.object(orchestrator, "run_mission_orchestrator_turn", side_effect=interrupt_consultation):
            result = agentdock.resolve_worker_consultation(
                plan,
                task,
                result={"consultation_id": consultation_id, "waiting_for_orchestrator": True},
            )

        self.assertTrue(result["paused"])
        self.assertTrue(result["deferred"])
        preserved_task = agentdock.one(
            "SELECT status,error,waiting_reason,finished_at FROM tasks WHERE id=?",
            (task_id,),
        )
        self.assertEqual(
            preserved_task,
            {
                "status": "waiting_for_orchestrator",
                "error": "",
                "waiting_reason": "Which contact behavior is allowed?",
                "finished_at": None,
            },
        )
        preserved_consultation = agentdock.one(
            "SELECT status,orchestrator_response_json,resolved_at FROM consultations WHERE id=?",
            (consultation_id,),
        )
        self.assertEqual(
            preserved_consultation,
            {"status": "queued", "orchestrator_response_json": "{}", "resolved_at": None},
        )
        self.assertEqual(
            agentdock.one("SELECT status,paused FROM plans WHERE id=?", (plan["id"],)),
            {"status": "pausing", "paused": 1},
        )

    def test_user_answer_is_persisted_before_same_thread_resolution(self):
        plan = self._orchestrator_plan("user-answer-plan")
        task_id = "user-answer-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json,worker_thread_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, plan["id"], 0, "Need a product choice", "Need a product choice", "researcher", "read", "[]", "waiting_for_user", json.dumps({}), "worker-thread"),
        )
        agentdock.execute(
            "INSERT INTO consultations(id,plan_id,task_id,status,question,reason,evidence_json,options_json,worker_thread_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("consult-user", plan["id"], task_id, "waiting_for_user", "Which link should be shown?", "No verified contact exists.", json.dumps(["No mailto link found"]), json.dumps(["Existing tool"]), "worker-thread", agentdock.now()),
        )
        agentdock.execute(
            "UPDATE plans SET pending_question_id=?,pending_question_json=?,status=? WHERE id=?",
            ("consult-user", json.dumps({"kind": "execution_question", "consultation_id": "consult-user", "question": "Which link should be shown?"}), "waiting_for_user", plan["id"]),
        )
        response = {"action": "answer_worker", "reason": "Use the existing tool.", "worker_message": "Link only to the existing tool.", "revised_contract": None, "questions": [], "evidence": []}
        with patch.object(
            orchestrator,
            "run_mission_orchestrator_turn",
            return_value={"text": json.dumps(response), "model": "gpt-5.6-sol", "thread_id": "mission-thread", "turn_id": "turn-answer"},
        ), patch.object(orchestrator, "claim_plan_run", return_value=False):
            result = agentdock.answer_consultation(plan["id"], "consult-user", "Use the existing tool only.")
        self.assertTrue(result["ok"], result)
        saved = agentdock.one("SELECT status,user_answer_json FROM consultations WHERE id=?", ("consult-user",))
        self.assertEqual(saved["status"], "resolved")
        self.assertIn("Use the existing tool only.", saved["user_answer_json"])
        self.assertEqual(agentdock.one("SELECT status,pending_question_id FROM plans WHERE id=?", (plan["id"],)), {"status": "approved", "pending_question_id": ""})


class MissionDispositionTests(AgentDockTestCase):
    def test_plan_update_preserves_orchestrator_thread_and_replaces_pending_graph(self):
        plan_id = self.add_plan("revise-plan", status="planned")
        agentdock.execute(
            "UPDATE plans SET decision=?,orchestrator_thread_id=? WHERE id=?",
            ("execute", "same-orchestrator-thread", plan_id),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("old-task", plan_id, 0, "Old task", "Old task", "pending"),
        )

        with patch.object(mission.threading, "Thread") as thread:
            result = mission.replan_mission(
                plan_id,
                mode="reconsider",
                user_note="Create a Markdown report and save it in the repository.",
            )

        self.assertEqual(result["status"], "planning")
        revised = agentdock.one(
            "SELECT status,decision,replan_note,orchestrator_thread_id FROM plans WHERE id=?",
            (plan_id,),
        )
        self.assertEqual(revised["status"], "planning")
        self.assertEqual(revised["decision"], "")
        self.assertIn("Create a Markdown report", revised["replan_note"])
        self.assertEqual(revised["orchestrator_thread_id"], "same-orchestrator-thread")
        self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", (plan_id,)), [])
        message = agentdock.one(
            "SELECT line FROM logs WHERE task_id=? AND stream='manual' ORDER BY id DESC LIMIT 1",
            ("orchestrator:" + plan_id,),
        )
        self.assertIn("Create a Markdown report", message["line"])
        thread.assert_called_once()

    def test_already_satisfied_creates_no_tasks(self):
        plan = self.build_disposition(
            "already",
            {"title": "Flag verification", "decision": "already_satisfied", "reason": "The requested flag is already present.", "evidence": ["Inspected src/app.py"], "final_response": "Nothing needs to change.", "questions": [], "tasks": []},
            goal="Add the flag",
        )
        self.assertEqual(plan["status"], "done")
        self.assertEqual(plan["title"], "Flag verification")
        self.assertEqual(plan["goal"], "Add the flag")
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
            expected_status = "done" if decision == "answer_only" else "waiting_for_permission"
            self.assertEqual(plan["status"], expected_status)
            self.assertEqual(agentdock.rows("SELECT * FROM tasks WHERE plan_id=?", (plan_id,)), [])

    def test_simple_execution_uses_one_task(self):
        plan = self.build_disposition(
            "one-task",
            {"decision": "execute", "reason": "One file must change.", "evidence": ["Target file is present."], "final_response": "", "questions": [], "tasks": [{"title": "Update one file", "agent_id": "coder", "mode": "write", "depends_on": [], "contract": self.planner_contract("Update one file", ["src/**"])}]},
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
        with patch.object(mission, "run_preflight", side_effect=AssertionError("zero-task mission entered preflight")):
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


class TimelineAndRuntimeControlTests(AgentDockTestCase):
    def test_plan_update_action_belongs_to_plan_review_not_agent_conversation(self):
        project_root = Path(__file__).resolve().parents[1]
        app_js = (project_root / "static" / "app.js").read_text()
        index_html = (project_root / "static" / "index.html").read_text()

        self.assertIn("openPlanUpdate", app_js)
        self.assertIn('id="planUpdateDialog"', index_html)
        self.assertNotIn("manualRevise", app_js)
        self.assertNotIn('id="manualRevise"', index_html)
        self.assertIn("Initialize Git repository", app_js)

    def test_timeline_merges_agent_events_logs_and_user_messages_in_sequence(self):
        plan_id = self.add_plan("timeline-plan", status="attention")
        task_id = "timeline-task"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Inspect", "Inspect", "done"),
        )
        session_id = agentdock.create_agent_session(
            plan_id, task_id, "worker", "gpt-5.6-luna", "medium", "default", "read", self.tmp,
        )
        agentdock.execute(
            "INSERT INTO agent_events(session_id,plan_id,task_id,ts,event_type,item_type,payload_json) VALUES(?,?,?,?,?,?,?)",
            (session_id, plan_id, task_id, 100, "item.completed", "agent_message", json.dumps({"item": {"type": "agent_message", "text": "Agent result"}})),
        )
        agentdock.execute(
            "INSERT INTO task_messages(id,task_id,plan_id,ts,text,status) VALUES(?,?,?,?,?,?)",
            ("timeline-message", task_id, plan_id, 101, "User follow-up", "delivered"),
        )
        agentdock.execute(
            "INSERT INTO logs(task_id,ts,stream,line) VALUES(?,?,?,?)",
            (task_id, 102, "system", "terminal result"),
        )

        timeline = agentdock.timeline_for(task_id)
        self.assertEqual([item["sequence"] for item in timeline["items"]], [1, 2, 3])
        self.assertEqual([item["kind"] for item in timeline["items"]], ["event", "message", "log"])
        self.assertEqual(timeline["items"][1]["text"], "User follow-up")

    def test_mission_config_applies_worker_overrides_only_to_remaining_tasks(self):
        plan_id = self.add_plan("config-plan", status="approved")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,status,model_override) VALUES(?,?,?,?,?,?,?,?,?)",
            ("config-pending", plan_id, 0, "Pending", "Pending", "coder", "write", "pending", ""),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,status,model_override) VALUES(?,?,?,?,?,?,?,?,?)",
            ("config-running", plan_id, 1, "Running", "Running", "coder", "write", "running", "old-model"),
        )
        result = agentdock.mission_config(
            plan_id,
            {
                "orchestrator_model": "gpt-5.6-sol",
                "orchestrator_effort": "low",
                "orchestrator_tier": "fast",
                "worker_model": "gpt-5.6-luna",
                "worker_effort": "low",
                "worker_tier": "fast",
                "max_parallel": 3,
                "apply_remaining": True,
            },
        )
        self.assertEqual(result["config"]["max_parallel"], 3)
        pending = agentdock.one("SELECT model_override,reasoning_effort_override,service_tier_override FROM tasks WHERE id=?", ("config-pending",))
        running = agentdock.one("SELECT model_override,reasoning_effort_override,service_tier_override FROM tasks WHERE id=?", ("config-running",))
        self.assertEqual(pending, {"model_override": "gpt-5.6-luna", "reasoning_effort_override": "low", "service_tier_override": "fast"})
        self.assertEqual(running["model_override"], "old-model")

    def test_mission_pause_and_resume_preserve_worker_thread_and_completed_tasks(self):
        plan_id = self.add_plan("pause-plan", status="running")
        agentdock.execute("UPDATE plans SET decision=? WHERE id=?", ("execute", plan_id))
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status,worker_thread_id) VALUES(?,?,?,?,?,?,?)",
            ("pause-done", plan_id, 0, "Done", "Done", "done", "done-thread"),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status,worker_thread_id) VALUES(?,?,?,?,?,?,?)",
            ("pause-running", plan_id, 1, "Running", "Running", "running", "worker-thread"),
        )
        paused = agentdock.pause_plan(plan_id)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "paused")
        self.assertEqual(agentdock.one("SELECT status,worker_thread_id FROM tasks WHERE id=?", ("pause-running",)), {"status": "paused_by_user", "worker_thread_id": "worker-thread"})

        with patch.object(mission, "claim_plan_run", return_value=False):
            resumed = agentdock.resume_plan(plan_id)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "approved")
        self.assertEqual(agentdock.one("SELECT status,worker_thread_id FROM tasks WHERE id=?", ("pause-running",)), {"status": "pending", "worker_thread_id": "worker-thread"})
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("pause-done",))["status"], "done")

    def test_no_task_resume_reopens_disposition_without_execution(self):
        plan_id = self.add_plan("no-task-resume", status="attention")
        agentdock.execute(
            "UPDATE plans SET decision=?,decision_reason=?,orchestrator_thread_id=? WHERE id=?",
            ("answer_only", "A previous answer was recorded.", "mission-thread", plan_id),
        )
        with patch.object(mission, "claim_plan_run", return_value=False):
            result = agentdock.resume_plan(plan_id)
        self.assertEqual(result["status"], "planning")
        reopened = agentdock.one(
            "SELECT status,decision,decision_reason,orchestrator_thread_id FROM plans WHERE id=?",
            (plan_id,),
        )
        self.assertEqual(reopened, {"status": "planning", "decision": "", "decision_reason": "", "orchestrator_thread_id": "mission-thread"})

    def test_planning_resume_releases_the_plan_lock_after_build_finishes(self):
        plan_id = self.add_plan("planning-resume-lock", status="paused")
        result = {
            "decision": "already_satisfied",
            "reason": "The requested planning check is already satisfied.",
            "evidence": ["Read-only planning evidence was collected."],
            "final_response": "No execution is needed.",
            "questions": [],
            "tasks": [],
        }
        with patch.object(mission, "quota_status", return_value={"status": "ok", "available": True}), patch.object(
            mission,
            "run_mission_orchestrator_turn",
            return_value={"text": json.dumps(result), "model": "gpt-5.6-sol", "thread_id": "planning-thread"},
        ):
            resumed = agentdock.resume_plan(plan_id)
            self.assertEqual(resumed["status"], "resuming")
            deadline = time.monotonic() + 3
            status = "planning"
            while time.monotonic() < deadline:
                status = agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"]
                if status != "planning":
                    break
                time.sleep(0.01)
            self.assertEqual(status, "done")

            acquired = False
            while time.monotonic() < deadline:
                if agentdock.claim_plan_run(plan_id):
                    acquired = True
                    agentdock.release_plan_run(plan_id)
                    break
                time.sleep(0.01)
            self.assertTrue(acquired, "planning resume left ACTIVE_PLAN_RUNS claimed")


class ExecutionTests(AgentDockTestCase):
    def test_verified_read_task_finishes_before_the_rest_of_its_parallel_wave(self):
        plan_id = self.add_plan("read-wave-plan", status="running")
        task_id = "read-wave-fast"
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, plan_id, 0, "Fast read", "Inspect", "researcher", "read", "[]", "pending", "{}"),
        )
        plan = agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))

        def completed_turn(*args, **kwargs):
            agentdock.execute(
                "UPDATE tasks SET status=?,output=?,finished_at=? WHERE id=?",
                ("executed", "verified result", agentdock.now(), task_id),
            )
            return {"ok": True, "output": "verified result"}

        with patch.object(tasks, "run_task_with_recovery", side_effect=completed_turn), patch.object(
            tasks, "drain_queued_manual_followups"
        ):
            result = tasks.run_parallel_task(
                plan,
                task,
                {"integration_workspace": self.tmp, "base_commit": "base"},
                "base",
            )

        persisted = agentdock.one("SELECT status,integration_status,finished_at FROM tasks WHERE id=?", (task_id,))
        self.assertTrue(result["ok"])
        self.assertEqual(persisted["status"], "done")
        self.assertEqual(persisted["integration_status"], "read_complete")
        self.assertIsNotNone(persisted["finished_at"])

    def test_initialize_git_repository_creates_empty_base_without_adding_user_files(self):
        workspace = self.tmp / "new-workspace"
        workspace.mkdir()
        (workspace / "research-notes.md").write_text("keep me untracked\n")

        snapshot = git_ops.initialize_git_repository(workspace)

        self.assertEqual(snapshot["classification"], "LOCAL_GIT")
        self.assertEqual(snapshot["branch"], "main")
        self.assertTrue(snapshot["head"])
        tracked = subprocess.run(
            ["git", "-C", str(workspace), "ls-files"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(tracked, "")
        self.assertIn("research-notes.md", snapshot["untracked_files"])

    def test_dirty_workspace_is_snapshotted_without_mutating_user_index(self):
        workspace = self.tmp / "dirty-snapshot"
        workspace.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(workspace)], check=True, capture_output=True, text=True)
        (workspace / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"],
            check=True, capture_output=True, text=True,
        )
        (workspace / "README.md").write_text("visible user edit\n")
        (workspace / "notes.txt").write_text("visible untracked note\n")
        subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True, capture_output=True, text=True)
        status_before = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1"],
            check=True, capture_output=True, text=True,
        ).stdout
        index_before = subprocess.run(
            ["git", "-C", str(workspace), "diff", "--cached", "--binary"],
            check=True, capture_output=True, text=True,
        ).stdout
        plan_id = self.add_plan("dirty-snapshot-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(workspace), plan_id))
        plan = agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))

        ctx = git_ops.prepare_integration(plan)
        try:
            self.assertEqual((ctx["integration_workspace"] / "README.md").read_text(), "visible user edit\n")
            self.assertEqual((ctx["integration_workspace"] / "notes.txt").read_text(), "visible untracked note\n")
            self.assertNotEqual(ctx["base_commit"], ctx["source_head"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "status", "--porcelain=v1"],
                    check=True, capture_output=True, text=True,
                ).stdout,
                status_before,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "diff", "--cached", "--binary"],
                    check=True, capture_output=True, text=True,
                ).stdout,
                index_before,
            )
        finally:
            mission.cleanup_successful_plan(ctx, plan_id)

    def test_missing_git_write_preflight_waits_with_initialize_action(self):
        workspace = self.tmp / "missing-git"
        workspace.mkdir()
        plan_id = self.add_plan("missing-git-plan", status="approved")
        agentdock.execute("UPDATE plans SET workspace=? WHERE id=?", (str(workspace), plan_id))
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("missing-git-write", plan_id, 0, "Write docs", "Write docs", "coder", "write", "[]", "pending", "{}"),
        )
        real_which = preflight.shutil.which
        with patch.object(preflight.shutil, "which", side_effect=lambda name: "/usr/bin/codex" if name == "codex" else real_which(name)):
            report = agentdock.run_preflight(
                agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)), True
            )

        self.assertEqual(report["status"], "ready")
        self.assertTrue(git_ops.repo_info(workspace)["is_git"])
        self.assertIn("Initialized a local Git repository with an empty base commit", report["repairs"])

    def test_initialize_git_action_replans_same_blocked_mission(self):
        workspace = self.tmp / "blocked-workspace"
        workspace.mkdir()
        plan_id = self.add_plan("blocked-no-git", status="blocked")
        agentdock.execute(
            "UPDATE plans SET workspace=?,decision=?,workspace_snapshot_json=? WHERE id=?",
            (str(workspace), "blocked", json.dumps({"classification": "NOT_GIT"}), plan_id),
        )
        with patch.object(mission, "replan_mission", return_value={"ok": True, "status": "planning"}) as replan:
            result = agentdock.apply_preflight_action(plan_id, "initialize_git")

        self.assertEqual(result["status"], "planning")
        self.assertTrue(git_ops.repo_info(workspace)["is_git"])
        replan.assert_called_once()
        self.assertEqual(replan.call_args.args[:2], (plan_id,))
        self.assertEqual(replan.call_args.kwargs["mode"], "reconsider")

    def test_parallel_write_claims_integration_before_validation(self):
        plan_id = self.add_plan("integration-ownership-before-validation", status="running")
        task_id = "integration-ownership-before-validation-task"
        agentdock.execute(
            """INSERT INTO tasks(
                id,plan_id,seq,title,instructions,agent_id,mode,status,contract_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                plan_id,
                0,
                "Bounded write",
                "Bounded write",
                "coder",
                "write",
                "pending",
                json.dumps({"objective": "Bounded write", "allowed_paths": ["src/**"]}),
            ),
        )
        plan = agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,))
        task = agentdock.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        observed_statuses = []

        def finish_worker(*args, **kwargs):
            agentdock.execute(
                "UPDATE tasks SET status=?,output=? WHERE id=?",
                ("executed", "worker result", task_id),
            )
            return {"ok": True, "output": "worker result"}

        def commit_during_integration(*args, **kwargs):
            observed_statuses.append(
                agentdock.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"]
            )
            return "worker-commit"

        with patch.object(
            tasks,
            "create_worker_worktree",
            return_value=(self.tmp / "worker-worktree", self.tmp / "worker-worktree", "worker-branch"),
        ), patch.object(tasks, "run_task_with_recovery", side_effect=finish_worker), patch.object(
            tasks, "commit_worker_changes", side_effect=commit_during_integration
        ):
            result = tasks.run_parallel_task(
                plan,
                task,
                {"integration_workspace": self.tmp},
                "base-commit",
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(observed_statuses, ["integrating"])
        self.assertEqual(
            agentdock.one("SELECT status,commit_hash FROM tasks WHERE id=?", (task_id,)),
            {"status": "integrating", "commit_hash": "worker-commit"},
        )

    def test_merge_conflict_resolver_continues_cherry_pick_and_preserves_clean_changes(self):
        repo = self.tmp / "conflict-with-clean-change-repo"
        repo.mkdir()

        def git(*args, check=True):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=check,
                capture_output=True,
                text=True,
            )

        git("init", "-b", "main")
        (repo / "a.txt").write_text("base A\n")
        (repo / "b.txt").write_text("base B\n")
        git("add", "a.txt", "b.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")

        git("checkout", "-b", "worker")
        (repo / "a.txt").write_text("worker A\n")
        (repo / "b.txt").write_text("worker B\n")
        git("add", "a.txt", "b.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "worker A + B")
        worker_commit = git("rev-parse", "HEAD").stdout.strip()

        git("checkout", "main")
        (repo / "a.txt").write_text("integration A\n")
        git("add", "a.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "integration A")

        plan_id = "conflict-preserve-clean-change"
        integration_dir = self.tmp / "integration-worktree"
        integration_branch = f"agentdock/{plan_id}/integration"
        git("worktree", "add", "-b", integration_branch, str(integration_dir), "main")
        try:
            def integration_git(*args, check=True):
                return subprocess.run(
                    ["git", "-C", str(integration_dir), *args],
                    check=check,
                    capture_output=True,
                    text=True,
                )

            agentdock.execute(
                "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,recovery_json) VALUES(?,?,?,?,?,?,?)",
                (plan_id, "Preserve clean cherry-pick changes", str(repo), "test", "running", agentdock.now(), json.dumps(agentdock.RECOVERY_DEFAULTS)),
            )
            task = {
                "id": "conflict-preserve-task",
                "plan_id": plan_id,
                "seq": 0,
                "title": "Resolve A without losing B",
                "contract_json": json.dumps({"allowed_paths": ["a.txt", "b.txt"]}),
            }
            agentdock.execute(
                "INSERT INTO tasks(id,plan_id,seq,title,instructions,mode,status,contract_json) VALUES(?,?,?,?,?,?,?,?)",
                (task["id"], plan_id, task["seq"], task["title"], task["title"], "write", "executed", task["contract_json"]),
            )

            def resolve_only_a(*args, **kwargs):
                (integration_dir / "a.txt").write_text("resolved A\n")
                return {"text": "Resolved only A; preserved the clean B application.", "model": "gpt-5.6-sol"}

            result = {
                "task": task,
                "ok": True,
                "commit": worker_commit,
                "wt": str(self.tmp / "unused-worker-worktree"),
                "branch": "worker",
            }
            recovered = integration.integrate_write_result(
                agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)),
                {"repo_root": repo, "integration_dir": integration_dir},
                result,
                orchestrator_turn=resolve_only_a,
            )

            self.assertTrue(recovered)
            self.assertEqual((integration_dir / "a.txt").read_text(), "resolved A\n")
            self.assertEqual((integration_dir / "b.txt").read_text(), "worker B\n")
            self.assertEqual(integration_git("status", "--porcelain").stdout, "")
            self.assertEqual(integration_git("log", "-1", "--pretty=%s").stdout.strip(), "worker A + B")
            task_state = agentdock.one(
                "SELECT status,integration_status FROM tasks WHERE id=?",
                (task["id"],),
            )
            self.assertEqual(task_state, {"status": "done", "integration_status": "resolved_by_orchestrator"})
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(integration_dir)],
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)

    def test_merge_conflict_resolver_rejects_unrelated_changes_before_staging(self):
        repo = self.tmp / "conflict-repo"
        repo.mkdir()

        def git(*args, check=True):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=check,
                capture_output=True,
                text=True,
            )

        git("init", "-b", "main")
        (repo / "conflict.txt").write_text("base\n")
        (repo / "unrelated.txt").write_text("base\n")
        git("add", "conflict.txt", "unrelated.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        git("checkout", "-b", "worker")
        (repo / "conflict.txt").write_text("worker change\n")
        git("add", "conflict.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "worker")
        worker_commit = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "main")
        (repo / "conflict.txt").write_text("integration change\n")
        git("add", "conflict.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "integration")

        plan_id = "conflict-resolver-scope"
        integration = self.tmp / "integration-worktree"
        branch = "agentdock/conflict-resolver-scope/integration"
        git("worktree", "add", "-b", branch, str(integration), "main")
        try:
            integration_git = lambda *args, **kwargs: subprocess.run(
                ["git", "-C", str(integration), *args],
                check=kwargs.get("check", True),
                capture_output=True,
                text=True,
            )
            integration_git("cherry-pick", worker_commit, check=False)
            self.assertTrue(integration_git("diff", "--name-only", "--diff-filter=U").stdout.strip())
            agentdock.execute(
                "INSERT INTO plans(id,goal,workspace,planner_engine,status,created_at,recovery_json) VALUES(?,?,?,?,?,?,?)",
                (plan_id, "Resolve the conflict", str(repo), "test", "running", agentdock.now(), json.dumps(agentdock.RECOVERY_DEFAULTS)),
            )
            task = {
                "id": "conflict-task",
                "plan_id": plan_id,
                "seq": 0,
                "title": "Resolve the bounded conflict",
                "contract_json": json.dumps({"allowed_paths": ["conflict.txt"]}),
            }
            result = {"task": task}

            def malicious_resolver(*args, **kwargs):
                (integration / "conflict.txt").write_text("resolved\n")
                (integration / "unrelated.txt").write_text("must not be included\n")
                return {"text": "Resolved.", "model": "gpt-5.6-sol"}

            with patch.object(orchestrator, "run_mission_orchestrator_turn", side_effect=malicious_resolver):
                recovered, detail = agentdock.resolve_merge_conflict(
                    agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)),
                    {"integration_dir": integration},
                    result,
                    "cherry-pick conflict",
                )
            self.assertFalse(recovered)
            self.assertIn("outside the conflicted set", detail)
            staged = integration_git("diff", "--cached", "--name-only").stdout
            self.assertNotIn("unrelated.txt", staged)
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(integration)], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)

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
            write_report = agentdock.run_preflight(
                agentdock.one("SELECT * FROM plans WHERE id=?", (write_plan_id,)), True
            )

        self.assertEqual(read_report["status"], "ready")
        self.assertTrue(any("read-only execution" in item for item in read_report["warnings"]))
        self.assertEqual(write_report["status"], "ready")
        self.assertTrue(any("orchestrator will retry" in item for item in write_report["warnings"]))
        self.assertTrue(lock.exists())

    def test_blocked_preflight_exposes_safe_actions_without_file_selection(self):
        plan_id = self.add_plan("missing-codex", status="approved")
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("missing-codex-task", plan_id, 0, "Inspect", "Inspect", "architect", "read", "[]", "pending", "{}"),
        )
        with patch.object(agentdock.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                agentdock.run_preflight(agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)), False)
        persisted = json.loads(agentdock.one(
            "SELECT preflight_json FROM plans WHERE id=?", (plan_id,)
        )["preflight_json"])
        self.assertEqual(persisted["status"], "attention")
        self.assertTrue(any("Codex CLI" in item for item in persisted["blockers"]))

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
        ), patch.object(tasks, "run_codex", return_value="read result"):
            agentdock.run_plan(plan_id, read_only_only=True)
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("mixed-read",))["status"], "done")
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", ("mixed-write",))["status"], "pending")
        plan = agentdock.one("SELECT status,preflight_status,summary FROM plans WHERE id=?", (plan_id,))
        self.assertEqual(plan["status"], "waiting_for_user")
        self.assertEqual(plan["preflight_status"], "")
        self.assertIn("Write tasks remain paused", plan["summary"])

    def test_waiting_consultation_does_not_pause_independent_read_task(self):
        plan_id = self.add_plan("parallel-consult", status="approved")
        consultation_task = "consult-waiting"
        independent_task = "independent-read"
        for values in [
            (consultation_task, plan_id, 0, "Needs a decision", "Needs a decision", "architect", "read", "[]", "waiting_for_orchestrator", "{}"),
            (independent_task, plan_id, 1, "Independent inspection", "Independent inspection", "researcher", "read", "[]", "pending", "{}"),
        ]:
            agentdock.execute(
                "INSERT INTO tasks(id,plan_id,seq,title,instructions,agent_id,mode,depends_json,status,contract_json,consultation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (*values, "queued-consult" if values[0] == consultation_task else ""),
            )
        agentdock.execute(
            "INSERT INTO consultations(id,plan_id,task_id,status,question,created_at) VALUES(?,?,?,?,?,?)",
            ("queued-consult", plan_id, consultation_task, "queued", "Which behavior?", agentdock.now()),
        )
        events = []

        def fake_parallel(plan, task, ctx, wave_base_commit):
            events.append("task")
            return {"task": task, "ok": True, "write": False, "output": "independent result"}

        def fake_resolve(plan, task, result=None, ctx=None, user_answer=None):
            events.append("consultation")
            return {"waiting_for_user": True}

        with patch.object(mission, "run_preflight", return_value={}), patch.object(
            mission, "run_parallel_task", side_effect=fake_parallel
        ), patch.object(mission, "resolve_worker_consultation", side_effect=fake_resolve):
            agentdock.run_plan(plan_id)

        self.assertEqual(events, ["task", "consultation"])
        self.assertEqual(agentdock.one("SELECT status FROM tasks WHERE id=?", (independent_task,))["status"], "done")

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
            report = agentdock.run_preflight(
                agentdock.one("SELECT * FROM plans WHERE id=?", (plan_id,)), True
            )
        self.assertEqual(exclude.read_text(), before)
        self.assertEqual((repo / "user-notes.txt").read_text(), "keep me\n")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(agentdock.one("SELECT preflight_status FROM plans WHERE id=?", (plan_id,))["preflight_status"], "ready")

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
        with patch.object(tasks, "run_codex", return_value="read result"):
            result = agentdock.run_parallel_task(plan, task, ctx, "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "read result")

    def test_failed_apply_can_be_retried_without_rerunning_completed_tasks(self):
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
        integration = config.WORKTREE_ROOT / plan_id / "integration"
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
            (plan_id, "apply", str(repo), "test", "attention", agentdock.now(), base_commit, str(integration), "failed"),
        )
        agentdock.execute(
            "INSERT INTO tasks(id,plan_id,seq,title,instructions,status) VALUES(?,?,?,?,?,?)",
            ("task-apply", plan_id, 0, "worker", "worker", "done"),
        )

        with patch.object(mission, "run_preflight", return_value={}):
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
    def contract(objective, allowed_paths):
        return {
            "objective": objective,
            "context": "Use the existing project and preserve unrelated behavior.",
            "scope": {"in_scope": [objective], "out_of_scope": ["Unrelated product changes"]},
            "allowed_paths": allowed_paths,
            "required_inputs": [],
            "implementation_steps": ["Inspect the bounded target", "Complete the contracted work"],
            "acceptance_criteria": ["The requested bounded outcome is complete"],
            "verification_commands": ["Run the focused verification"],
            "expected_output": ["A concise result and verification summary"],
            "escalation_conditions": ["Escalate ambiguity instead of guessing"],
            "decision_policy": "Do not broaden scope; ask the orchestrator when a material decision is required."
        }
    result = {"tasks": [
        {"title": "Inspect repository", "agent_id": "architect", "mode": "read", "depends_on": [], "contract": contract("Inspect repository", ["workspace/**"])},
        {"title": "Write focused marker", "agent_id": "coder", "mode": "write", "depends_on": [0], "contract": contract("Write focused marker", ["src/**"])},
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
            with patch.object(mission, "run_preflight", side_effect=AssertionError("mission execution entered preflight")), patch.object(
                mission, "quota_status", return_value={"status": "ok", "available": True}
            ):
                agentdock.build_plan(plan_id)
                agentdock.execute("UPDATE plans SET status='approved',approved_at=? WHERE id=?", (agentdock.now(), plan_id))
                agentdock.run_plan(plan_id)
            self.assertEqual(agentdock.one("SELECT status FROM plans WHERE id=?", (plan_id,))["status"], "done")
            plan = agentdock.one("SELECT status,applied,apply_status FROM plans WHERE id=?", (plan_id,))
            self.assertEqual(plan["status"], "done")
            self.assertEqual(plan["applied"], 1)
            self.assertEqual(plan["apply_status"], "applied")
            self.assertEqual((repo / "src" / "agentdock-fake.txt").read_text(), "integrated\n")
        finally:
            os.environ["PATH"] = previous_path


if __name__ == "__main__":
    unittest.main()
