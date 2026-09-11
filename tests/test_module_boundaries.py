import ast
import unittest
from pathlib import Path
from unittest.mock import patch

import agentdock


class ModuleBoundaryTests(unittest.TestCase):
    @staticmethod
    def _local_imports(module_name):
        """Return fully-qualified modules imported by one package module."""
        package_root = Path(__file__).parents[1] / "agentdock"
        source = (package_root / f"{module_name}.py").read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = "agentdock"
                if node.level > 1:
                    base += "." + ".".join([".."] * (node.level - 1))
                if node.module:
                    imported.add(f"{base}.{node.module}")
                else:
                    imported.update(f"{base}.{alias.name}" for alias in node.names)
            elif node.module:
                imported.add(node.module)
        return imported

    def test_runtime_symbols_have_the_expected_owner(self):
        self.assertIs(agentdock.run_codex, agentdock.codex.run_codex)
        self.assertIs(agentdock.run_mission_orchestrator_turn, agentdock.orchestrator.run_mission_orchestrator_turn)
        self.assertIs(agentdock.run_plan, agentdock.mission.run_plan)
        self.assertIs(agentdock.run_manual_followup, agentdock.tasks.run_manual_followup)
        self.assertIs(agentdock.timeline_for, agentdock.timeline.timeline_for)
        self.assertIs(agentdock.Handler, agentdock.api.Handler)
        self.assertIs(agentdock.main, agentdock.app.main)

    def test_facade_patches_are_local_to_the_patched_owner(self):
        replacement = object()
        with patch.object(agentdock, "run_codex", replacement):
            self.assertIs(agentdock.run_codex, replacement)
            self.assertIsNot(agentdock.codex.run_codex, replacement)
            self.assertIsNot(agentdock.tasks.run_codex, replacement)
        with patch.object(agentdock.codex, "run_codex", replacement):
            self.assertIs(agentdock.codex.run_codex, replacement)

    def test_script_entrypoint_is_a_thin_facade(self):
        entrypoint = Path(__file__).parents[1] / "agentdock.py"
        self.assertLess(len(entrypoint.read_text().splitlines()), 40)
        self.assertIn("from agentdock.app import main", entrypoint.read_text())

    def test_layer_boundaries_have_no_upward_imports(self):
        forbidden = {
            "tasks": {"agentdock.orchestrator"},
            "codex": {"agentdock.mission"},
            "git_ops": {"agentdock.mission", "agentdock.tasks", "agentdock.orchestrator"},
            "db": {
                "agentdock.api",
                "agentdock.codex",
                "agentdock.git_ops",
                "agentdock.mission",
                "agentdock.orchestrator",
                "agentdock.preflight",
                "agentdock.tasks",
                "agentdock.timeline",
            },
            "api": {"agentdock.codex", "agentdock.db", "agentdock.git_ops"},
        }
        for module_name, disallowed in forbidden.items():
            imports = self._local_imports(module_name)
            self.assertFalse(
                imports & disallowed,
                f"{module_name}.py imports an upper layer: {sorted(imports & disallowed)}",
            )

    def test_facades_do_not_propagate_module_globals(self):
        package_source = (Path(__file__).parents[1] / "agentdock" / "__init__.py").read_text()
        entrypoint_source = (Path(__file__).parents[1] / "agentdock.py").read_text()
        for source in (package_source, entrypoint_source):
            self.assertNotIn("__dict__.update", source)
            self.assertNotIn("ModuleType", source)
            self.assertNotIn("globals().update", source)

    def test_version_has_one_runtime_source(self):
        from agentdock.version import VERSION

        self.assertEqual(agentdock.VERSION, VERSION)
        self.assertEqual(VERSION, "0.13.0")
        root = Path(__file__).parents[1]
        self.assertIn(f'version = "{VERSION}"', (root / "pyproject.toml").read_text())
        self.assertIn(f"AgentDock v{VERSION}", (root / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
