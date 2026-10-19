#!/usr/bin/env python3
"""Debug Runner CLI 的不启动服务器测试。"""

from __future__ import annotations

import locale
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script" / "debug_runner.py"


class DebugRunnerCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            timeout=30,
            check=False,
        )

    def test_help_documents_task_and_unsafe_actions(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--task", result.stdout)
        self.assertIn("--unsafe-actions", result.stdout)
        self.assertIn("默认: src/minecraft", result.stdout)

    def test_list_tasks_does_not_start_environment(self) -> None:
        result = self.run_cli("--list-tasks")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trade", result.stdout)
        self.assertIn("mining", result.stdout)

    def test_task_is_required(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("必须提供 --task", result.stderr)


if __name__ == "__main__":
    unittest.main()
