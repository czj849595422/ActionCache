#!/usr/bin/env python3
"""COAP request assembly tests."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SkillRegistry  # noqa: E402
from src.baseline.COAP.config import COAPConfig  # noqa: E402
from src.baseline.COAP.prompts import COAPPromptBuilder  # noqa: E402
from src.baseline.COAP.request_builder import build_planning_request  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.tasks import TaskSpec  # noqa: E402


def make_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "coap.request.test",
            "category": "test",
            "name": "COAP Request Test",
            "agents": {"count": 1, "names": ["a0"]},
            "completion": {
                "timeout_seconds": 10,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


class COAPRequestBuilderTests(unittest.TestCase):
    def test_completed_action_result_description_enters_current_action(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        entry = memory.append_history_event(
            agent_id="a0", source="self", event_type="planning",
            content={"standard_plan": "combat.observe", "selected_action": "combat.observe", "reasoning": "scan"},
        )
        memory.update_history_event(
            entry.entry_id,
            content={"result_data": {"action": "combat.observe", "ok": True}, "result_description": "侦查完成：发现 1 个agent。", "result_status": "completed"},
        )

        request = build_planning_request(
            prompt_builder=COAPPromptBuilder(),
            memory=memory,
            spec=make_spec(),
            registry=SkillRegistry(allowed_actions={"combat.observe"}),
            instructions=None,
            config=COAPConfig(cache_boot={"mode": "empty"}),
            agent_id="a0",
            teammates=[],
            operation="standard_action",
            cache_candidates=(),
            last_error=None,
        )

        user = request.messages[1]["content"]
        self.assertIn("【历史信息】", user)
        self.assertIn("普通规划：combat.observe", user)
        self.assertIn("状态：completed", user)
        self.assertIn("侦查完成：发现 1 个agent。", user)

    def test_running_action_result_is_not_rendered(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        entry = memory.append_history_event(
            agent_id="a0", source="self", event_type="planning",
            content={"standard_plan": "wait.wait", "selected_action": "wait.wait"},
        )
        memory.update_history_event(
            entry.entry_id,
            content={"result_data": {"action": "wait.wait", "ok": True}, "result_description": "done", "result_status": None},
        )

        request = build_planning_request(
            prompt_builder=COAPPromptBuilder(),
            memory=memory,
            spec=make_spec(),
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
            instructions=None,
            config=COAPConfig(cache_boot={"mode": "empty"}),
            agent_id="a0",
            teammates=[],
            operation="standard_action",
            cache_candidates=(),
            last_error=None,
        )

        self.assertNotIn("结果: done", request.messages[1]["content"])

    def test_team_messages_merge_recent_results_and_chats_by_time(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        memory.append_history_event(agent_id="a1", source="teammate", event_type="execution", content={"selected_action": "old", "result_description": "old result", "result_status": "completed"})
        memory.append_history_event(agent_id="a1", source="teammate", event_type="execution", content={"selected_action": "old", "result_description": "result 1", "result_status": "completed"})
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "own", "result_description": "own result", "result_status": "completed"})
        memory.append_history_event(agent_id="a2", source="teammate", event_type="communication", content={"message": "chat 1"})
        memory.append_history_event(agent_id="system", source="system", event_type="system", content={"message": "system note"})
        memory.append_history_event(agent_id="a1", source="teammate", event_type="execution", content={"selected_action": "new", "result_description": "result 2", "result_status": "completed"})

        request = build_planning_request(
            prompt_builder=COAPPromptBuilder(),
            memory=memory,
            spec=make_spec(),
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
            instructions=None,
            config=COAPConfig(cache_boot={"mode": "empty"}, chat_window=4),
            agent_id="a0",
            teammates=["a1", "a2"],
            operation="standard_action",
            cache_candidates=(),
            last_error=None,
        )

        user = request.messages[1]["content"]
        self.assertNotIn("old result", user)
        expected = [
            "动作完成结果：old（状态：completed）",
            "own result",
            "通信：chat 1",
            "system note",
            "动作完成结果：new（状态：completed）",
        ]
        positions = [user.index(item) for item in expected]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
