"""Trade-only recipe context and public purchase announcements."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory import TeamMemory, render_history  # noqa: E402
from src.agents.action import SimpleInstructionRegistry, SkillRegistry, render_action_space  # noqa: E402
from src.tasks import create_default_registry, task_action_space  # noqa: E402
from src.tasks.trade import (  # noqa: E402
    TradeRecipeHistory,
    append_trade_recipe_section,
    filter_trade_recipe_history,
    format_trade_purchase_broadcast,
    load_trade_config,
    render_trade_prompt_context,
)


CONFIG_PATH = ROOT / "src" / "tasks" / "specs" / "resources" / "trade.trade_config.yaml"


class TradePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history = TradeRecipeHistory(load_trade_config(CONFIG_PATH))

    @staticmethod
    def query_result(*, recipes: list[dict] | None = None, ok: bool = True) -> dict:
        return {
            "ok": ok,
            "result": {
                "ok": ok,
                "recipes": recipes if recipes is not None else [{"ingredients": []}],
            },
        }

    def test_recipe_history_is_private_and_deduplicated(self) -> None:
        self.assertTrue(
            self.history.record_action_result(
                "Alice",
                "crafting.query_recipe",
                {"item": "oak_planks"},
                self.query_result(),
                "completed",
            )
        )
        self.history.record_action_result(
            "Alice",
            "crafting.query_recipe",
            {"item": "minecraft:oak_planks"},
            self.query_result(),
            "completed",
        )
        self.assertIn("【已查询配方】", self.history.render("Alice"))
        self.assertEqual(self.history.render("Alice").count("oak_planks："), 1)
        self.assertEqual(self.history.render("Bob"), "")

    def test_failed_empty_and_unconfigured_queries_are_ignored(self) -> None:
        cases = (
            ("oak_planks", self.query_result(ok=False), "completed"),
            ("oak_planks", self.query_result(recipes=[]), "completed"),
            ("not_a_trade_item", self.query_result(), "completed"),
            ("oak_planks", self.query_result(), "failed"),
        )
        for item, result, status in cases:
            self.history.record_action_result(
                "Alice", "crafting.query_recipe", {"item": item}, result, status
            )
        self.assertEqual(self.history.render("Alice"), "")

    def test_completed_query_history_is_synced_and_deduplicated(self) -> None:
        result = self.query_result()
        result.update(
            {
                "action": "crafting.query_recipe",
                "status": "completed",
                "raw": {"params": {"item": "oak_planks"}},
            }
        )
        entries = [
            {
                "agent_id": "Alice",
                "content": {
                    "result_data": result,
                    "result_status": "completed",
                },
            },
            {
                "agent_id": "Alice",
                "content": {
                    "result_data": result,
                    "result_status": "completed",
                },
            },
            {
                "agent_id": "Bob",
                "content": {
                    "result_data": result,
                    "result_status": "completed",
                },
            },
        ]

        self.history.sync_from_history("Alice", entries)

        rendered = self.history.render("Alice")
        self.assertIn("【已查询配方】", rendered)
        self.assertEqual(rendered.count("oak_planks："), 1)
        self.assertEqual(self.history.render("Bob"), "")

    def test_recipe_history_is_appended_to_user_message(self) -> None:
        result = self.query_result()
        result.update(
            {
                "action": "crafting.query_recipe",
                "status": "completed",
                "raw": {"params": {"item": "oak_planks"}},
            }
        )
        spec = SimpleNamespace(
            category="trade",
            params={"trade_config": load_trade_config(CONFIG_PATH).to_dict()},
        )
        messages = append_trade_recipe_section(
            (
                {"role": "system", "content": "system"},
                {"role": "user", "content": "current state"},
            ),
            spec=spec,
            agent_id="Alice",
            history=self.history,
            history_entries=[
                {
                    "agent_id": "Alice",
                    "content": {
                        "result_data": result,
                        "result_status": "completed",
                    },
                }
            ],
        )

        self.assertIn("【已查询配方】", messages[1]["content"])
        self.assertIn("oak_planks：", messages[1]["content"])

    def test_recipe_query_is_kept_out_of_truncated_history(self) -> None:
        entries = [
            {
                "content": {
                    "selected_action": "crafting.query_recipe",
                    "result_description": "large recipe payload",
                }
            },
            {"content": {"selected_action": "trade.inspect_shop"}},
        ]
        filtered = filter_trade_recipe_history(entries)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["content"]["selected_action"], "trade.inspect_shop")

    def test_purchase_broadcast_is_strict_and_history_is_public(self) -> None:
        result = {
            "ok": True,
            "result": {
                "ok": True,
                "buyer": "A",
                "seller": "B",
                "purchased": {"item": "minecraft:iron_ingot", "count": 3},
            },
        }
        self.assertEqual(
            format_trade_purchase_broadcast(
                action="trade.buy_from_shop", result=result, status="completed"
            ),
            "A从B购买3个iron_ingot。",
        )
        self.assertIsNone(
            format_trade_purchase_broadcast(
                action="trade.buy_from_shop", result=result, status="failed"
            )
        )

        memory = TeamMemory()
        memory.append_history_event(
            agent_id="A",
            source="self",
            event_type="planning",
            content={
                "selected_action": "buy(B, iron_ingot, 3)",
                "communication": "A从B购买3个iron_ingot。",
                "public_broadcast": "A从B购买3个iron_ingot。",
                "result_status": "completed",
                "result_description": "private details",
                "result_data": {"secret": True},
            },
        )
        rendered = render_history(memory.prompt_history("B", team_limit=10, system_limit=10))
        self.assertIn("A从B购买3个iron_ingot。", rendered)
        self.assertNotIn("private details", rendered)
        self.assertNotIn("secret", rendered)

    def test_only_trade_action_space_contains_recipe_query(self) -> None:
        registry = create_default_registry()
        self.assertIn("crafting.query_recipe", task_action_space(registry.get("trade")))
        self.assertNotIn("navigation.goto_position", task_action_space(registry.get("trade")))
        self.assertNotIn("crafting.query_recipe", task_action_space(registry.get("pvp")))

    def test_trade_quantity_parameters_are_rendered(self) -> None:
        registry = SkillRegistry(
            allowed_actions={
                "trade.buy_from_shop",
                "trade.craft_target",
            }
        )
        catalog = render_action_space(
            registry,
            SimpleInstructionRegistry(skill_registry=registry),
            "zh",
        )
        self.assertIn("buy(shop_name, item, count)", catalog)
        self.assertIn("craft_target(item, count)", catalog)
        self.assertIn("从shop_name购买count个item。", catalog)
        self.assertIn("合成count个item。", catalog)
        self.assertNotIn("count为购买数量", catalog)
        self.assertNotIn("count为合成数量", catalog)

    def test_runtime_trade_config_renders_prompt_context(self) -> None:
        config = load_trade_config(CONFIG_PATH)
        spec = SimpleNamespace(category="trade", params={"trade_config": config.to_dict()})
        text = render_trade_prompt_context(spec, agent_id="Alice")
        self.assertIn("名称：Alice", text)
        self.assertIn("【其他 Agent 职业】", text)
        self.assertIn("目标清单：", text)


if __name__ == "__main__":
    unittest.main()
