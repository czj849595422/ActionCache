"""AutoGen prompt construction tests."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.AutoGen import A2AStore, AutoGenConfig  # noqa: E402
from src.baseline.AutoGen.perception import render_commander_perception  # noqa: E402
from src.baseline.AutoGen.prompts import (  # noqa: E402
    build_commander_request,
    build_executor_request,
    render_public_results,
)
from src.baseline.AutoGen.schemas import SubtaskAssignment  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.tasks.spec import load_spec  # noqa: E402
from src.tasks.trade import load_trade_config  # noqa: E402


def fake_spec(**overrides):
    values = {
        "task_id": "monster_clearance",
        "description": "YAML preset task description",
        "category": "monster_clearance",
        "observation_range": 20,
        "load_description_text": lambda: "external description file text",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AutoGenPromptTests(unittest.TestCase):
    def test_commander_prompt_uses_autogen_commander_description(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "plain observation")
        request = build_commander_request(
            memory=memory,
            a2a=A2AStore(agents=("Alice",)),
            spec=fake_spec(),
            config=AutoGenConfig(),
            eligible_recipients=("Alice",),
        )
        user = request.messages[1]["content"]
        self.assertIn("你是怪物清剿任务的 Commander", user)
        self.assertIn("没有 Minecraft 实体", user)
        self.assertNotIn("YAML preset task description", user)
        self.assertNotIn("external description file text", user)
        self.assertNotIn("合理使用自身资源", user)

    def test_executor_prompt_uses_task_folder_description(self) -> None:
        spec = load_spec("src/tasks/monster_clearance/config/0.yaml")
        memory = TeamMemory()
        memory.update_observation("Alice", "obs")
        request = build_executor_request(
            memory=memory,
            a2a=A2AStore(agents=("Alice",)),
            spec=spec,
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
            instructions=SimpleInstructionRegistry(),
            config=AutoGenConfig(),
            agent_id="Alice",
            teammates=("Bob",),
            assignment=SubtaskAssignment(
                recipient="Alice",
                subtask_id="s1",
                objective="巡查附近区域",
                success_criteria="汇报附近是否有怪物",
            ),
        )
        user = request.messages[1]["content"]
        self.assertIn("你处在一支负责清剿怪物的队伍中", user)
        self.assertIn("僵尸可能出现精英怪", user)
        self.assertNotIn("你是怪物清剿任务的 Commander", user)

    def test_public_results_use_team_message_window(self) -> None:
        memory = TeamMemory()
        memory.append_history_event(
            agent_id="Alice", source="teammate", event_type="communication",
            content={"message": "moving"},
        )
        memory.append_history_event(
            agent_id="Bob", source="teammate", event_type="execution",
            content={"selected_action": "wait", "result_description": "done", "result_status": "completed"},
        )

        text = render_public_results(memory, limit=20)

        self.assertIn("[队友: Alice]", text)
        self.assertIn("通信：moving", text)
        self.assertIn("[队友: Bob]", text)
        self.assertIn("动作完成结果：wait（状态：completed）", text)

    def test_commander_perception_summarizes_and_deduplicates(self) -> None:
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            """
            【自身】
            Alice；生命值 20/20；饥饿值 20/20；位置 (0.0,0.0,0.0)；状态：位于地面。
            【手持与装备】
            主手: stone_sword；副手: 无；胸部: iron_chestplate。
            【背包】
            arrow×16；bow×1；空槽 32/36。
            【其他玩家】
            Bob；位置 (1.0,0.0,0.0)；距离 1.0 格。
            【敌对实体】
            zombie_1；位置 (5.0,0.0,5.0)；距离 7.0 格；方向：前方。
            skeleton_9；位置 (25.0,0.0,0.0)；距离 25.0 格；方向：前方。
            【附近掉落物】
            bread×2；位置 (3.0,0.0,0.0)；距离 3.0 格。
            """,
        )
        memory.update_observation(
            "Bob",
            """
            【自身】
            Bob；生命值 18/20；饥饿值 19/20；位置 (1.0,0.0,0.0)；状态：位于地面。
            【手持与装备】
            主手: bow；副手: 无；脚部: iron_boots。
            【背包】
            arrow×12；bread×1；空槽 31/36。
            【敌对实体】
            zombie_1；位置 (5.0,0.0,5.0)；距离 6.5 格；方向：右前方。
            zombie_2；位置 (10.0,0.0,0.0)；距离 9.0 格；方向：前方。
            【附近掉落物】
            bread×2；位置 (3.0,0.0,0.0)；距离 2.0 格。
            potion×1；位置 (30.0,0.0,0.0)；距离 30.0 格。
            """,
        )
        text = render_commander_perception(memory=memory, spec=fake_spec())

        self.assertIn("Agents:", text)
        self.assertIn("- Alice: 自身: Alice；生命值 20/20", text)
        self.assertIn("- Bob: 自身: Bob；生命值 18/20", text)
        self.assertNotIn("其他玩家", text)
        self.assertEqual(text.count("zombie_1"), 1)
        self.assertIn("zombie_2", text)
        self.assertNotIn("skeleton_9", text)
        self.assertEqual(text.count("bread×2"), 1)
        self.assertNotIn("potion×1", text)
        self.assertIn("seen_by=Alice,Bob", text)

    def test_trade_commander_gets_professions_without_target_lists(self) -> None:
        spec = load_spec(ROOT / "src" / "tasks" / "specs" / "trade.yaml")
        config = load_trade_config(ROOT / "src" / "tasks" / "specs" / "resources" / "trade.trade_config.yaml")
        memory = TeamMemory()
        request = build_commander_request(
            memory=memory,
            a2a=A2AStore(agents=tuple(agent.name for agent in config.agents)),
            spec=spec,
            config=AutoGenConfig(),
            eligible_recipients=tuple(agent.name for agent in config.agents),
        )
        user = request.messages[1]["content"]
        for agent in config.agents:
            self.assertIn(agent.name, user)
            self.assertIn(config.profession_map[agent.profession].label, user)
            for target in agent.targets:
                self.assertNotIn(target.item.split(":", 1)[-1], user)
        self.assertNotIn("目标清单", user)


if __name__ == "__main__":
    unittest.main()
