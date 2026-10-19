#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一任务环境 demo 的只读 CLI 测试。"""

from __future__ import annotations

import locale
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script" / "demo_task_environment.py"


class TaskEnvironmentDemoTests(unittest.TestCase):
    def run_demo(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            timeout=30,
            check=False,
        )

    def test_list_tasks_exposes_all_builtin_environments(self) -> None:
        result = self.run_demo("--list-tasks")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boss_combat", result.stdout)
        self.assertIn("mining", result.stdout)
        self.assertIn("monster_clearance", result.stdout)
        self.assertIn("trade", result.stdout)

    def test_check_accepts_builtin_task_id(self) -> None:
        result = self.run_demo(
            "--task",
            "mining",
            "--check",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("任务校验通过: mining", result.stdout)
        self.assertIn("world=base_env", result.stdout)

    def test_check_accepts_yaml_path(self) -> None:
        task_path = (
            ROOT
            / "src"
            / "tasks"
            / "specs"
            / "monster_clearance.yaml"
        )
        result = self.run_demo("--task", str(task_path), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("任务校验通过: monster_clearance", result.stdout)

    def test_task_is_required_outside_list_mode(self) -> None:
        result = self.run_demo("--check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("必须提供 --task", result.stderr)

    def test_check_accepts_trade_task_and_world(self) -> None:
        result = self.run_demo("--task", "trade", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("任务校验通过: trade", result.stdout)
        self.assertIn("world=trade", result.stdout)

    def test_help_exposes_debug_controls(self) -> None:
        result = self.run_demo("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--debug", result.stdout)
        self.assertNotIn("--keep-server", result.stdout)
        self.assertIn("默认: src/minecraft", result.stdout)


if __name__ == "__main__":
    unittest.main()
