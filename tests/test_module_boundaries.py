import unittest
from pathlib import Path
from unittest.mock import patch

import agentdock


class ModuleBoundaryTests(unittest.TestCase):
    def test_runtime_symbols_have_the_expected_owner(self):
        self.assertIs(agentdock.run_codex, agentdock.codex.run_codex)
        self.assertIs(agentdock.run_mission_orchestrator_turn, agentdock.orchestrator.run_mission_orchestrator_turn)
        self.assertIs(agentdock.run_plan, agentdock.mission.run_plan)
        self.assertIs(agentdock.run_manual_followup, agentdock.tasks.run_manual_followup)
        self.assertIs(agentdock.timeline_for, agentdock.timeline.timeline_for)
        self.assertIs(agentdock.Handler, agentdock.api.Handler)
        self.assertIs(agentdock.main, agentdock.app.main)

    def test_legacy_facade_patches_reach_extracted_modules(self):
        replacement = object()
        with patch.object(agentdock, "run_codex", replacement):
            self.assertIs(agentdock.codex.run_codex, replacement)
            self.assertIs(agentdock.tasks.run_codex, replacement)
        self.assertIs(agentdock.run_codex, agentdock.codex.run_codex)

    def test_script_entrypoint_is_a_thin_facade(self):
        entrypoint = Path(__file__).parents[1] / "agentdock.py"
        self.assertLess(len(entrypoint.read_text().splitlines()), 40)
        self.assertIn("from agentdock.app import main", entrypoint.read_text())


if __name__ == "__main__":
    unittest.main()
