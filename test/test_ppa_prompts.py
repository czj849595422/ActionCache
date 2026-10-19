#!/usr/bin/env python3
"""Text-only PPA prompt assembly tests."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.Parallelized import (  # noqa: E402
    JsonDecisionCodec,
    PPAConfig,
    PPAPromptBuilder,
)
from src.memory import TeamMemory  # noqa: E402
from src.tasks import TaskSpec, create_default_registry  # noqa: E402


def make_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "ppa.prompt.test",
            "category": "test",
            "name": "Prompt Test",
            "description": "完成测试目标",
            "agents": {"count": 2, "names": ["a0", "a1"]},
            "completion": {
                "timeout_seconds": 10,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


class PPAPromptBuilderTests(unittest.TestCase):
    def test_prompt_is_text_only_and_respects_allowlist_and_chat_window(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "生命值 20，附近有僵尸。")
        memory.append_history_event(agent_id="a1", source="teammate", event_type="communication", content={"message": "old"})
        memory.append_history_event(agent_id="a1", source="teammate", event_type="communication", content={"message": "middle"})
        memory.append_history_event(agent_id="a0", source="self", event_type="communication", content={"message": "new"})
        action = memory.append_history_event(
            agent_id="a0", source="self", event_type="planning",
            content={"standard_plan": "wait.wait(seconds=1)", "selected_action": "wait.wait(seconds=1)", "reasoning": "观察环境"},
        )
        memory.update_history_event(
            action.entry_id,
            content={"result_data": {"ok": False, "error": "timeout", "result": {"internal": "must not dump"}}, "result_description": "等待 指定 秒失败：timeout", "result_status": "failed"},
        )
        registry = SkillRegistry(allowed_actions={"wait.wait", "chat.say"})
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=memory,
            codec=JsonDecisionCodec(),
            config=PPAConfig(chat_window=2),
            task_state_provider=lambda: {
                "outcome": {"status": "running"},
                "metrics": {"remaining": 2},
            },
        )

        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=registry,
            last_error="上一输出缺少 MESSAGE",
        )

        self.assertEqual(len(request.messages), 2)
        self.assertEqual(request.source.component, "planning_worker")
        self.assertEqual(request.source.operation, "plan")
        self.assertEqual(request.source.task_id, "ppa.prompt.test")
        self.assertEqual(request.source.agent_id, "a0")
        self.assertTrue(all(isinstance(item["content"], str) for item in request.messages))
        system = request.messages[0]["content"]
        user = request.messages[1]["content"]
        self.assertIn("wait.wait", system)
        self.assertIn("chat.say", system)
        self.assertNotIn("combat.fight", system)
        self.assertIn("生命值 20", user)
        self.assertNotIn("old", user)
        self.assertIn("new", user)
        self.assertIn("wait.wait(seconds=1)", user)
        self.assertIn("等待 指定 秒失败：timeout", user)
        self.assertNotIn("must not dump", user)
        self.assertIn("上一输出缺少 MESSAGE", user)
        self.assertNotIn("任务状态", user)
        self.assertEqual(request.extra_body, {})
        self.assertNotIn("response_format", request.parameters)

        retry_request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=registry,
            error_history=("格式错误", "参数错误"),
        )
        retry_user = retry_request.messages[1]["content"]
        self.assertIn("第 1 次失败：格式错误", retry_user)
        self.assertIn("第 2 次失败：参数错误", retry_user)

    def test_missing_observation_and_action_have_text_fallbacks(self) -> None:
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(),
            config=PPAConfig(),
        )
        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
        )
        user = request.messages[1]["content"]
        self.assertIn("尚无有效观测", user)
        self.assertIn("【历史信息】", user)
        self.assertIn("暂无历史信息", user)

    def test_simple_instruction_catalog_format(self) -> None:
        instructions = SimpleInstructionRegistry()
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(instructions=instructions),
            config=PPAConfig(),
            instructions=instructions,
        )
        registry = SkillRegistry(
            allowed_actions={
                "chat.say",
                "wait.wait",
                "navigation.goto_position",
            }
        )
        request = builder.build(agent_id="a0", teammates=["a1"], registry=registry)
        system = request.messages[0]["content"]
        self.assertIn("chat(message) : #", system)
        self.assertIn("wait(seconds) : #", system)
        self.assertIn("goto(position/entity_name) : #", system)
        self.assertNotIn("chat.say(", system)
        self.assertNotIn("combat.", system)

    def test_simple_instruction_catalog_english_and_variadic(self) -> None:
        instructions = SimpleInstructionRegistry()
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(instructions=instructions),
            config=PPAConfig(language="en"),
            instructions=instructions,
        )
        registry = SkillRegistry(
            allowed_actions={
                "trade.take_from_container",
                "container.pick_object_from_chest",
                "chat.say",
            }
        )
        request = builder.build(agent_id="a0", teammates=["a1"], registry=registry)
        system = request.messages[0]["content"]
        self.assertIn(
            "- take_from(container, item, num, ...) : #Take num_X of item_X from container.",
            system,
        )
        self.assertIn(
            "- pick_from_chest(position/chest/chest_id, item, num, ...) : "
            "#Take num_X of item_X from the chest at position={x, y, z}/chest/chest_id.",
            system,
        )
        self.assertIn(
            "- chat(message) : #Send message to communicate.",
            system,
        )

    def test_running_action_is_not_hidden_by_a_new_planned_proposal(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        memory.append_history_event(
            agent_id="a0", source="self", event_type="planning",
            content={"standard_plan": "wait.wait(seconds=10)", "selected_action": "wait.wait(seconds=10)"},
        )
        memory.append_history_event(
            agent_id="a0", source="self", event_type="planning",
            content={"standard_plan": "chat.say(message='next')", "selected_action": "chat.say(message='next')"},
        )
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=memory,
            codec=JsonDecisionCodec(),
            config=PPAConfig(),
        )

        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=SkillRegistry(allowed_actions={"wait.wait", "chat.say"}),
        )
        user = request.messages[1]["content"]
        self.assertIn("普通规划：wait.wait(seconds=10)", user)
        self.assertIn("普通规划：chat.say(message='next')", user)
        self.assertIn("chat.say(message='next')", user)

    def test_result_channel_included_in_team_messages(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "wait", "result_description": "auto result", "result_status": "completed"})
        memory.append_history_event(agent_id="a1", source="teammate", event_type="communication", content={"message": "real message"})
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=memory,
            codec=JsonDecisionCodec(),
            config=PPAConfig(chat_window=5),
        )
        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=SkillRegistry(allowed_actions={"chat.say"}),
        )
        user = request.messages[1]["content"]
        self.assertIn("real message", user)
        self.assertIn("auto result", user)

    def test_custom_template_dir_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "system.txt").write_text(
                "SYS {catalog}|{protocol}",
                encoding="utf-8",
            )
            (tmp / "user.txt").write_text(
                "USR {agent_id}|{observation_text}",
                encoding="utf-8",
            )
            builder = PPAPromptBuilder(
                spec=make_spec(),
                memory=TeamMemory(),
                codec=JsonDecisionCodec(),
                config=PPAConfig(),
                template_dir=tmp,
            )
            request = builder.build(
                agent_id="a0",
                teammates=["a1"],
                registry=SkillRegistry(allowed_actions={"chat.say"}),
            )
            self.assertTrue(
                request.messages[0]["content"].startswith("SYS ")
            )
            self.assertIn("chat.say(message)", request.messages[0]["content"])
            self.assertTrue(
                request.messages[1]["content"].startswith("USR ")
            )
            self.assertIn("a0|", request.messages[1]["content"])

    def test_missing_language_template_falls_back(self) -> None:
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(),
            config=PPAConfig(language="en"),
        )
        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=SkillRegistry(allowed_actions={"chat.say"}),
        )
        self.assertIn("【动作空间】", request.messages[0]["content"])

    def test_task_description_loaded_from_task_directory(self) -> None:
        spec = create_default_registry().get("mining")
        self.assertIsNotNone(spec.description_file)
        self.assertEqual(spec.description_file.name, "mining.description.txt")
        text = spec.load_description_text()
        self.assertIsNotNone(text)
        self.assertIn("你是一名矿工", text)
        self.assertIn("lapis_block(1)", text)

    def test_task_description_injected_into_system(self) -> None:
        spec = create_default_registry().get("mining")
        builder = PPAPromptBuilder(
            spec=spec,
            memory=TeamMemory(),
            codec=JsonDecisionCodec(),
            config=PPAConfig(),
        )
        request = builder.build(
            agent_id="Alice",
            teammates=["Bob", "Carol", "Dave"],
            registry=SkillRegistry(allowed_actions={"chat.say"}),
        )
        system = request.messages[0]["content"]
        self.assertIn("你是一名矿工", system)
        self.assertIn("netherite_block(20)", system)
        self.assertIn("【动作空间】", system)

    def test_task_description_does_not_fallback_to_spec_description(self) -> None:
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(),
            config=PPAConfig(),
        )
        request = builder.build(
            agent_id="a0",
            teammates=["a1"],
            registry=SkillRegistry(allowed_actions={"chat.say"}),
        )
        self.assertNotIn("完成测试目标", request.messages[0]["content"])

    def test_passive_communication_template_injects_feedback(self) -> None:
        spec = create_default_registry().get("mining")
        memory = TeamMemory()
        memory.update_observation("Alice", "附近有 iron_block。")
        builder = PPAPromptBuilder(
            spec=spec,
            memory=memory,
            codec=JsonDecisionCodec(),
            config=PPAConfig(language="zh"),
        )
        request = builder.build_passive(
            agent_id="Alice",
            teammates=["Bob", "Carol", "Dave"],
            last_action_result=(
                "Alice：执行mine_iron(position={'x': 1, 'y': 2, 'z': 3}) 完成"
            ),
        )
        self.assertEqual(len(request.messages), 2)
        self.assertEqual(request.source.component, "planning_worker")
        self.assertEqual(request.source.operation, "passive_communication")
        system = request.messages[0]["content"]
        user = request.messages[1]["content"]
        self.assertIn("你是一名矿工", system)
        self.assertIn("请发起一条通信用于信息共享", system)
        self.assertIn("附近有 iron_block", user)
        self.assertIn("【上一轮动作与结果】", user)
        self.assertIn("执行mine_iron", user)

    def test_passive_communication_falls_back_without_feedback(self) -> None:
        builder = PPAPromptBuilder(
            spec=make_spec(),
            memory=TeamMemory(),
            codec=JsonDecisionCodec(),
            config=PPAConfig(),
        )
        request = builder.build_passive(
            agent_id="a0",
            teammates=["a1"],
            last_action_result="",
        )
        self.assertIn("暂无动作结果", request.messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
