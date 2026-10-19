"""PPA 动作执行记录表写入测试。"""

from __future__ import annotations

import csv
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.Parallelized.schemas import PPAConfig, PPAProposal  # noqa: E402
from src.baseline.Parallelized.workers import ActingWorker  # noqa: E402
from src.interface import ActionUnit  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.recording.table import ActionExecutionTable  # noqa: E402
from src.runtime import SingleSlotOverwriteBuffer  # noqa: E402


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def execute_unit(self, unit):
        return SimpleNamespace(
            ok=True,
            status="completed",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "completed", "description": "ok"},
            raw={"ok": True},
        )

    def start_unit(self, unit):
        return SimpleNamespace(
            ok=True,
            status="running",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "running"},
            raw={"ok": True},
        )

    def status(self, action_id: str):
        return SimpleNamespace(
            ok=True,
            status="completed",
            result={"ok": True, "status": "completed"},
            raw={"ok": True},
        )

    def stop(self, action_id: str, reason: str = "stop"):
        return SimpleNamespace(
            ok=True,
            status="interrupted",
            result={"ok": True, "status": "interrupted"},
            raw={"ok": True},
        )


class FakeAgent:
    def __init__(self, username: str, registry: SkillRegistry) -> None:
        self.username = username
        self.action_planner = FakePlanner(registry)


class PPAActingExecutionTableTests(unittest.TestCase):
    def test_execution_row_written_with_plan_source_empty(self) -> None:
        from src.tasks import create_default_registry, task_action_space

        spec = create_default_registry().get("mining")
        registry = SkillRegistry(
            allowed_actions=task_action_space(spec)
        )
        agent = FakeAgent("Alice", registry)
        memory = TeamMemory()
        buffer = SingleSlotOverwriteBuffer()
        interrupt_event = threading.Event()
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            table = ActionExecutionTable(Path(tmp) / "actions.csv")
            worker = ActingWorker(
                agent=agent,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=memory,
                config=PPAConfig(),
                instructions=SimpleInstructionRegistry(),
                broadcast_results=False,
                execution_table=table,
            )
            proposal = PPAProposal(
                action_unit=ActionUnit(
                    agent_id="Alice",
                    action="wait.wait",
                    params={"seconds": 1},
                    mode="continuous",
                ),
                interrupt=False,
                reason="r",
                passive_message=None,
                raw_text="raw",
                planning_started_at=100.0,
                planning_finished_at=102.0,
            )
            buffer.put(proposal)
            worker.iterate()
            worker.iterate()
            table.close()
            rows = list(
                csv.reader(
                    (Path(tmp) / "actions.csv").open(encoding="utf-8-sig")
                )
            )
            self.assertEqual(len(rows), 2)
            row = rows[1]
            self.assertEqual(row[1], "Alice")
            self.assertIn("wait.wait", row[2])
            self.assertEqual(row[4], "100.0")
            self.assertEqual(row[5], "102.0")
            self.assertEqual(row[11], "False")
            self.assertIn("状态 completed", row[12])
            self.assertEqual(row[13], "")


if __name__ == "__main__":
    unittest.main()
