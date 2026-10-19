#!/usr/bin/env python3
"""Integration checks for JavaScript combat target resolution."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "test" / "js" / "test_combat_skill.js"


class CombatSkillIntegrationTests(unittest.TestCase):
    def test_node_combat_target_resolution(self) -> None:
        result = subprocess.run(
            ["node", str(NODE_TEST)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("combat target and consumable tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
