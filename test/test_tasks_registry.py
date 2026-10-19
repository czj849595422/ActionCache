#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/tasks 注册表测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    BaseTaskManager,
    BossCombatManager,
    MonsterClearanceManager,
    PVPManager,
    TradeTaskManager,
    TaskRegistry,
    TaskRegistryError,
    create_default_registry,
)


class TaskRegistryTests(unittest.TestCase):
    def test_default_registry_loads_builtin_specs(self) -> None:
        registry = create_default_registry()
        spec = registry.get("boss_combat")
        self.assertEqual(spec.category, "boss_combat")
        self.assertIn("boss_combat", [item.task_id for item in registry.list()])
        self.assertEqual(
            registry.categories(),
            ["boss_combat", "debug", "mining", "monster_clearance", "pvp", "trade"],
        )
        monster = registry.get("monster_clearance")
        self.assertEqual(monster.category, "monster_clearance")
        self.assertEqual(registry.get("trade").category, "trade")
        debug_spec = registry.get("debug")
        self.assertEqual(debug_spec.category, "debug")

    def test_manager_for_registered_category(self) -> None:
        registry = create_default_registry()
        factory = registry.manager_for("boss_combat")
        manager = factory()
        self.assertIsInstance(manager, BossCombatManager)
        self.assertIsInstance(registry.manager_for("debug")(), BaseTaskManager)
        self.assertIsInstance(
            registry.manager_for("monster_clearance")(),
            MonsterClearanceManager,
        )
        self.assertIsInstance(registry.manager_for("pvp")(), PVPManager)
        self.assertIsInstance(registry.manager_for("trade")(), TradeTaskManager)

    def test_manager_for_unknown_category_raises(self) -> None:
        registry = create_default_registry()
        with self.assertRaises(TaskRegistryError):
            registry.manager_for("missing_category")

    def test_duplicate_task_id_raises(self) -> None:
        registry = create_default_registry()
        spec = registry.get("boss_combat")
        with self.assertRaises(TaskRegistryError):
            registry.register_spec(spec)

    def test_get_unknown_task_raises(self) -> None:
        registry = create_default_registry()
        with self.assertRaises(TaskRegistryError):
            registry.get("missing.task")

    def test_load_from_file(self) -> None:
        registry = TaskRegistry()
        path = ROOT / "src" / "tasks" / "specs" / "boss_combat.yaml"
        spec = registry.load(path)
        self.assertEqual(registry.get(spec.task_id).task_id, spec.task_id)

    def test_duplicate_manager_raises_without_overwrite(self) -> None:
        registry = TaskRegistry()
        registry.register_manager("boss_combat", BossCombatManager)
        with self.assertRaises(TaskRegistryError):
            registry.register_manager("boss_combat", BossCombatManager)
        registry.register_manager("boss_combat", BossCombatManager, overwrite=True)

    def test_non_callable_factory_raises(self) -> None:
        registry = TaskRegistry()
        with self.assertRaises(TaskRegistryError):
            registry.register_manager("boss_combat", "not-callable")


if __name__ == "__main__":
    unittest.main()
