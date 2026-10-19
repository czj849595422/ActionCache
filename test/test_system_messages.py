#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统消息 worker 的离线单元测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory import TeamMemory  # noqa: E402
from src.tasks import SystemMessageWorker  # noqa: E402


class FakeManager:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.contexts = []

    def system_message(self, context):
        self.contexts.append(context)
        if not self.responses:
            return None
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FailingMemory:
    def append_history_event(self, *args, **kwargs):
        raise RuntimeError("memory unavailable")


class SystemMessageWorkerTests(unittest.TestCase):
    def test_manager_interval_caps_worker_interval(self) -> None:
        manager = FakeManager([])
        manager.system_message_interval_seconds = 10.0

        worker = SystemMessageWorker(
            manager=manager,
            context_provider=lambda: SimpleNamespace(),
            memory=TeamMemory.create(),
            interval=30.0,
        )

        self.assertEqual(worker.loop_interval, 10.0)

    def test_text_is_written_to_memory_and_broadcast(self) -> None:
        memory = TeamMemory.create()
        broadcasts: list[str] = []
        events: list[tuple[str, dict]] = []
        context = SimpleNamespace(task_id="task.1")
        worker = SystemMessageWorker(
            manager=FakeManager(["  Boss Health: 300  "]),
            context_provider=lambda: context,
            memory=memory,
            interval=1.0,
            broadcast=broadcasts.append,
            event_sink=lambda event_type, **data: events.append((event_type, data)),
        )

        worker.iterate()

        chats = memory.prompt_history("system", team_limit=1, system_limit=1)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["source"], "system")
        self.assertEqual(chats[0]["content"]["message"], "Boss Health: 300")
        self.assertEqual(broadcasts, ["Boss Health: 300"])
        self.assertEqual(worker.published_count, 1)
        self.assertEqual(events[-1][0], "system_message.published")

    def test_none_and_empty_messages_are_ignored(self) -> None:
        memory = TeamMemory.create()
        broadcasts: list[str] = []
        manager = FakeManager([None, "", "   "])
        worker = SystemMessageWorker(
            manager=manager,
            context_provider=lambda: SimpleNamespace(),
            memory=memory,
            interval=1.0,
            broadcast=broadcasts.append,
        )

        worker.iterate()
        worker.iterate()
        worker.iterate()

        self.assertEqual(memory.prompt_history("system"), [])
        self.assertEqual(broadcasts, [])
        self.assertEqual(worker.published_count, 0)

    def test_manager_failure_is_non_fatal(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = SystemMessageWorker(
            manager=FakeManager([RuntimeError("read failed")]),
            context_provider=lambda: SimpleNamespace(),
            memory=TeamMemory.create(),
            interval=1.0,
            event_sink=lambda event_type, **data: events.append((event_type, data)),
        )

        worker.iterate()

        self.assertEqual(worker.published_count, 0)
        self.assertEqual(events[0][0], "system_message.failed")
        self.assertEqual(events[0][1]["stage"], "read")

    def test_memory_and_broadcast_failures_are_non_fatal(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = SystemMessageWorker(
            manager=FakeManager(["message"]),
            context_provider=lambda: SimpleNamespace(),
            memory=FailingMemory(),
            interval=1.0,
            broadcast=lambda _: (_ for _ in ()).throw(RuntimeError("rcon failed")),
            event_sink=lambda event_type, **data: events.append((event_type, data)),
        )

        worker.iterate()

        stages = [
            data["stage"]
            for event_type, data in events
            if event_type == "system_message.failed"
        ]
        self.assertEqual(stages, ["memory", "broadcast"])
        self.assertEqual(worker.published_count, 0)


if __name__ == "__main__":
    unittest.main()
