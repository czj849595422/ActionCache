"""统一历史信息注入契约测试。"""

from __future__ import annotations

import unittest

from src.memory import TeamMemory, render_history
from src.agents.action import SimpleInstructionRegistry


class HistoryInjectionTests(unittest.TestCase):
    def test_empty_coap_planning_entry_is_not_injected(self) -> None:
        rendered = render_history(
            [
                {
                    "source": "self",
                    "event_type": "planning",
                    "content": {
                        "coap_record": True,
                        "cache_plan": None,
                        "standard_plan": None,
                        "selected_action": None,
                        "reasoning": None,
                        "communication": None,
                        "result_description": "",
                        "result_status": None,
                    },
                }
            ]
        )

        self.assertEqual(rendered, "暂无历史信息。")

    def test_history_action_uses_simple_alias_and_supplied_arguments(self) -> None:
        memory = TeamMemory()
        entry = memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="planning",
            content={
                "standard_plan": "wait.wait",
                "selected_action": "wait.wait",
                "params": {"seconds": 1},
            },
        )

        rendered = render_history(
            memory.prompt_history("Alice"),
            instructions=SimpleInstructionRegistry(),
        )

        self.assertIn("普通规划：wait(seconds=1)", rendered)
        self.assertIn("动作选择：wait(seconds=1)", rendered)
        self.assertNotIn("wait.wait", rendered)
        self.assertEqual(memory.get(entry.entry_id).sequence, entry.sequence)

    def test_team_and_system_windows_are_independent_then_merged_by_time(self) -> None:
        memory = TeamMemory()
        memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="communication",
            content={"message": "self-1"},
        )
        memory.append_history_event(
            agent_id="Bob",
            source="self",
            event_type="communication",
            content={"message": "teammate-1"},
        )
        memory.append_history_event(
            agent_id="system",
            source="system",
            event_type="system",
            content={"message": "system-1"},
        )
        memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="communication",
            content={"message": "self-2"},
        )
        memory.append_history_event(
            agent_id="system",
            source="system",
            event_type="system",
            content={"message": "system-2"},
        )

        entries = memory.prompt_history("Alice", team_limit=2, system_limit=1)
        self.assertEqual(
            [item["content"]["message"] for item in entries],
            ["teammate-1", "self-2", "system-2"],
        )
        self.assertEqual(entries[0]["source"], "teammate")
        self.assertEqual(entries[1]["source"], "self")
        self.assertEqual(entries[2]["source"], "system")

    def test_self_result_is_private_but_teammate_view_is_sanitized(self) -> None:
        memory = TeamMemory()
        entry = memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="planning",
            content={
                "standard_plan": "mine.go",
                "reasoning": "private reasoning",
                "selected_action": "mine.go",
                "communication": "ore found",
                "result_description": "detailed private description",
                "result_status": "completed",
                "result_data": {"secret": True},
            },
        )
        own = render_history(memory.prompt_history("Alice"))
        self.assertIn("private reasoning", own)
        self.assertIn("detailed private description", own)
        self.assertIn("result_data", str(memory.prompt_history("Alice")[0]["content"]))

        teammate = render_history(memory.prompt_history("Bob"))
        self.assertIn("[队友: Alice]", teammate)
        self.assertIn("动作完成结果：mine.go（状态：completed）", teammate)
        self.assertIn("通信：ore found", teammate)
        self.assertNotIn("private reasoning", teammate)
        self.assertNotIn("detailed private description", teammate)
        self.assertNotIn("secret", teammate)
        self.assertEqual(memory.get(entry.entry_id).sequence, entry.sequence)

    def test_pending_history_keeps_original_sequence_when_completed(self) -> None:
        memory = TeamMemory()
        first = memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="planning",
            content={"standard_plan": "wait.wait", "result_status": None},
        )
        second = memory.append_history_event(
            agent_id="Bob",
            source="self",
            event_type="communication",
            content={"message": "later"},
        )
        updated = memory.update_history_event(
            first.entry_id,
            content={
                "result_description": "done",
                "result_status": "completed",
                "result_data": {"ok": True},
            },
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.sequence, first.sequence)
        self.assertGreater(second.sequence, updated.sequence)

    def test_coap_record_keeps_two_plans_and_only_exposes_result_to_teammates(self) -> None:
        memory = TeamMemory()
        entry = memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="planning",
            content={
                "coap_record": True,
                "cache_plan": "move.forward(distance=2)",
                "standard_plan": "mine.break(block='stone')",
                "selected_action": "move.forward(distance=2)",
                "selected_source": "cache",
                "reasoning": "普通规划推理",
                "communication": "已选择缓存动作",
                "result_description": "到达目标位置，详细执行说明",
                "result_status": "completed",
                "result_data": {"internal": "private"},
            },
        )
        own = render_history(memory.prompt_history("Alice"))
        self.assertIn("缓存规划：move.forward(distance=2)", own)
        self.assertIn("普通规划：mine.break(block='stone')", own)
        self.assertIn("动作选择：move.forward(distance=2)（来源：cache）", own)
        self.assertIn("推理内容：普通规划推理", own)
        self.assertIn("到达目标位置，详细执行说明", own)

        teammate = render_history(memory.prompt_history("Bob"))
        self.assertIn("动作完成结果：move.forward(distance=2)（状态：completed）", teammate)
        self.assertNotIn("普通规划推理", teammate)
        self.assertNotIn("详细执行说明", teammate)
        self.assertNotIn("internal", teammate)
        self.assertEqual(memory.get(entry.entry_id).sequence, entry.sequence)


if __name__ == "__main__":
    unittest.main()
