"""任务级动作默认值辅助（apply_task_action_defaults）测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import (  # noqa: E402
    SimpleInstructionRegistry,
    SkillRegistry,
    render_action_space,
)
from src.agents.action.task_defaults import apply_task_action_defaults  # noqa: E402
from src.tasks import (  # noqa: E402
    create_default_registry,
    task_action_space,
)


class FakeActionUnit:
    def __init__(self, action: str, params: dict) -> None:
        self.action = action
        self.params = params


class FakeProposal:
    def __init__(self, action: str, params: dict, input_params: dict) -> None:
        self.action_unit = FakeActionUnit(action, params)
        self.input_params = dict(input_params)


class ApplyTaskActionDefaultsTests(unittest.TestCase):
    def test_clearance_fight_defaults_to_none(self) -> None:
        proposal = FakeProposal(
            "combat.fight",
            {"collect_drops": "optional"},
            {},
        )
        apply_task_action_defaults("monster_clearance", proposal)
        self.assertEqual(proposal.action_unit.params["collect_drops"], "none")

    def test_explicit_collect_drops_preserved(self) -> None:
        proposal = FakeProposal(
            "combat.fight",
            {"collect_drops": "optional"},
            {"collect_drops": "required"},
        )
        apply_task_action_defaults("monster_clearance", proposal)
        self.assertEqual(proposal.action_unit.params["collect_drops"], "optional")

    def test_non_clearance_untouched(self) -> None:
        proposal = FakeProposal(
            "combat.fight",
            {"collect_drops": "optional"},
            {},
        )
        apply_task_action_defaults("mining", proposal)
        self.assertEqual(proposal.action_unit.params["collect_drops"], "optional")

    def test_non_fight_untouched(self) -> None:
        proposal = FakeProposal("inventory.pick", {"positions": []}, {})
        apply_task_action_defaults("monster_clearance", proposal)
        self.assertEqual(proposal.action_unit.params["positions"], [])


class InventoryActionCatalogTests(unittest.TestCase):
    def test_clearance_catalog_includes_drop_and_give(self) -> None:
        spec = create_default_registry().get("monster_clearance")
        registry = SkillRegistry(
            allowed_actions=task_action_space(spec)
        )
        catalog = render_action_space(
            registry,
            SimpleInstructionRegistry(),
            "zh",
        )
        self.assertIn("drop(items)", catalog)
        self.assertIn("give(target, items)", catalog)
        self.assertIn("丢弃items中的物品到当前位置地面。", catalog)
        self.assertIn("靠近target并把items中的物品丢给对方拾取。", catalog)
        self.assertNotIn("Drop the given items", catalog)
        self.assertNotIn("Walk close to the target player", catalog)

    def test_task_catalogs_do_not_use_known_english_fallbacks(self) -> None:
        known_english_fallbacks = (
            "Drop the given items",
            "Walk close to the target player",
        )
        instructions = SimpleInstructionRegistry()
        for spec in create_default_registry().list():
            allowed_actions = task_action_space(spec)
            if allowed_actions is None:
                continue
            with self.subTest(task_id=spec.task_id):
                catalog = render_action_space(
                    SkillRegistry(allowed_actions=allowed_actions),
                    instructions,
                    "zh",
                )
                for phrase in known_english_fallbacks:
                    self.assertNotIn(phrase, catalog)


if __name__ == "__main__":
    unittest.main()
