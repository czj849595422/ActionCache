"""P0-5: per-task minimal skill action-space tests."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SkillRegistry  # noqa: E402
from src.tasks import (  # noqa: E402
    create_default_registry,
    skill_agent_options,
    TaskSpec,
    TaskSpecError,
    task_action_space,
)


COMMON = frozenset(
    {
        "chat.say",
        "chat.whisper",
        "navigation.goto_position",
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
        "wait.wait",
    }
)
EXPECTED = {
    "mining": COMMON
    - {
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
    }
    | {
        "mining.mine_diamond",
        "mining.mine_emerald",
        "mining.mine_gold",
        "mining.mine_iron",
        "mining.mine_lapis",
        "mining.mine_netherite",
        "equipment.equip_items",
        "equipment.unequip_items",
    },
    "monster_clearance": COMMON
    - {
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
    }
    | {
        "combat.observe",
        "combat.retreat",
        "combat.fight",
        "combat.use_food",
        "equipment.equip_items",
        "equipment.unequip_items",
        "inventory.pick",
        "inventory.drop",
        "inventory.give",
    },
    "boss_combat": COMMON
    - {
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
    }
    | {
        "combat.observe",
        "combat.fight",
        "combat.use_food",
        "equipment.equip_items",
        "equipment.unequip_items",
        "inventory.pick",
        "inventory.drop",
        "inventory.give",
    },
    "pvp": COMMON
    - {
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
    }
    | {
        "combat.observe",
        "combat.retreat",
        "combat.fight",
        "combat.use_food",
        "equipment.equip_items",
        "equipment.unequip_items",
        "inventory.pick",
        "inventory.drop",
        "inventory.give",
    },
    "trade": COMMON
    - {
        "navigation.goto_position",
        "navigation.follow_position",
        "navigation.follow_entity",
        "navigation.goto_block",
        "navigation.explore",
    }
    | {
        "trade.inspect_own_containers",
        "trade.inspect_shop",
        "trade.transfer_owned_items",
        "trade.take_from_container",
        "trade.store_to_container",
        "trade.buy_from_shop",
        "trade.craft_target",
        "crafting.query_recipe",
    },
}

TASK_IDS = {
    "mining": "mining",
    "monster_clearance": "monster_clearance",
    "boss_combat": "boss_combat",
    "pvp": "pvp",
    "trade": "trade",
}


class TaskAllowlistTests(unittest.TestCase):
    def test_each_task_config_has_expected_action_space(self) -> None:
        registry = create_default_registry()
        for task_id, expected in EXPECTED.items():
            with self.subTest(task_id=task_id):
                self.assertEqual(task_action_space(registry.get(task_id)), expected)

    def test_common_contract_for_every_configured_task(self) -> None:
        for spec in create_default_registry().list():
            action_space = task_action_space(spec)
            if action_space is None:
                continue
            allowlist = action_space
            self.assertIn("wait.wait", allowlist)
            self.assertIn("chat.say", allowlist)
            self.assertIn("chat.whisper", allowlist)
            self.assertNotIn("container.register_chests", allowlist)
            allowed_inventory = {"inventory.pick", "inventory.drop", "inventory.give"}
            self.assertFalse(
                any(
                    action.startswith("inventory.")
                    and action not in allowed_inventory
                    for action in allowlist
                )
            )

    def test_mining_excludes_block_mine(self) -> None:
        allowlist = task_action_space(create_default_registry().get("mining"))
        self.assertIsNotNone(allowlist)
        self.assertNotIn("block.mine", allowlist)
        self.assertTrue(
            any(
                action.startswith("mining.mine_")
                for action in allowlist
            )
        )

    def test_requested_navigation_actions_are_removed(self) -> None:
        registry = create_default_registry()
        for task_id in ("boss_combat", "monster_clearance"):
            with self.subTest(task_id=task_id):
                actions = task_action_space(registry.get(task_id))
                self.assertNotIn("navigation.follow_entity", actions)
                self.assertNotIn("navigation.follow_position", actions)

        for task_id in ("mining", "trade"):
            with self.subTest(task_id=task_id):
                actions = task_action_space(registry.get(task_id))
                for action in (
                    "navigation.explore",
                    "navigation.follow_entity",
                    "navigation.follow_position",
                    "navigation.goto_block",
                ):
                    self.assertNotIn(action, actions)

    def test_boss_combat_excludes_retreat(self) -> None:
        registry = create_default_registry()
        self.assertNotIn(
            "combat.retreat",
            task_action_space(registry.get("boss_combat")),
        )
        self.assertIn(
            "combat.retreat",
            task_action_space(registry.get("monster_clearance")),
        )
        self.assertIn("combat.retreat", task_action_space(registry.get("pvp")))

    def test_pvp_excludes_large_navigation_and_auto_equip_actions(self) -> None:
        allowlist = task_action_space(create_default_registry().get("pvp"))
        self.assertIsNotNone(allowlist)
        for action in (
            "navigation.follow_position",
            "navigation.follow_entity",
            "navigation.goto_block",
            "navigation.explore",
            "equipment.auto_equip_combat",
        ):
            self.assertNotIn(action, allowlist)

    def test_debug_empty_task_allows_all_actions(self) -> None:
        registry = create_default_registry()
        spec = registry.get("debug")
        self.assertEqual(spec.category, "debug")
        self.assertIsNone(task_action_space(spec))
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            options = skill_agent_options(spec, root, root / "chests.json")
            self.assertIsNone(options["allowed_actions"])
            self.assertEqual(options["internal_actions"], frozenset())

    def test_skill_agent_options_uses_task_action_space(self) -> None:
        registry = create_default_registry()
        for category, task_id in TASK_IDS.items():
            with self.subTest(category=category):
                spec = registry.get(task_id)
                with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
                    root = Path(directory)
                options = skill_agent_options(spec, root, root / "chests.json")
                self.assertEqual(options["allowed_actions"], task_action_space(spec))
                self.assertEqual(
                    options["bridge_env"]["MC_OBSERVATION_RANGE"],
                    str(spec.observation_range),
                )
                if category in {"pvp", "monster_clearance", "boss_combat"}:
                    self.assertTrue(options["bridge_env"]["EQUIPMENT_REGISTRY_FILE"].endswith("equipment_registry.json"))
                else:
                    self.assertNotIn("EQUIPMENT_REGISTRY_FILE", options["bridge_env"])
                expected_internal = (
                    {"container.register_chests"}
                    if category == "trade"
                    else frozenset()
                )
                self.assertEqual(options["internal_actions"], expected_internal)

    def test_registry_resolves_only_task_allowed_actions(self) -> None:
        allowlist = task_action_space(create_default_registry().get("mining"))
        registry = SkillRegistry(
            allowed_actions=allowlist
        )
        registry.resolve("mining.mine_diamond")
        with self.assertRaises(PermissionError):
            registry.resolve("block.break")
        with self.assertRaises(PermissionError):
            registry.resolve("combat.attack")
        with self.assertRaises(PermissionError):
            registry.resolve("block.mine")

    def test_unsafe_actions_still_exclude_internal(self) -> None:
        registry = create_default_registry()
        spec = registry.get("trade")
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            options = skill_agent_options(
                spec,
                root,
                root / "chests.json",
                unsafe_actions=True,
            )
            self.assertIn("container.inspect_chest", options["allowed_actions"])
            self.assertNotIn("container.register_chests", options["allowed_actions"])

    def test_generated_task_id_uses_its_task_action_space(self) -> None:
        base = create_default_registry().get("boss_combat").to_dict()
        base["task_id"] = "boss_combat.42"
        base["name"] = "Generated Boss Combat"
        narrowed = TaskSpec.from_dict(base)

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            options = skill_agent_options(narrowed, root, root / "chests.json")

        self.assertEqual(
            options["allowed_actions"],
            task_action_space(create_default_registry().get("boss_combat")),
        )

    def test_unknown_task_id_does_not_inherit_category_action_space(self) -> None:
        base = create_default_registry().get("mining").to_dict()
        base["task_id"] = "custom_mining.42"
        custom = TaskSpec.from_dict(base)

        with self.assertRaises(TaskSpecError):
            task_action_space(custom)


if __name__ == "__main__":
    unittest.main()
