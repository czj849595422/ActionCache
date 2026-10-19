"""Boss combat initialization config generator tests."""

from __future__ import annotations

import math
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import load_spec  # noqa: E402
from src.tasks.boss_combat import (  # noqa: E402
    BossCombatGenerationSpec,
    generate_agent_spawn_positions,
    generate_boss_initial_loadout,
    generate_boss_spawn_position,
    generate_task_batch,
)
from src.tasks.boss_combat.arena import (  # noqa: E402
    BOSS_SPAWN_MAX_X,
    BOSS_SPAWN_MAX_Y,
    BOSS_SPAWN_MAX_Z,
    BOSS_SPAWN_MIN_X,
    BOSS_SPAWN_MIN_Y,
    BOSS_SPAWN_MIN_Z,
)


class BossCombatGeneratorTests(unittest.TestCase):
    @staticmethod
    def _enchantments(item: str) -> dict[str, int]:
        return {
            name: int(level)
            for name, level in re.findall(
                r'id:"minecraft:([^"]+)",lvl:(\d+)s', item
            )
        }

    def test_random_initial_loadout_respects_enchantment_rules(self) -> None:
        loadouts = [generate_boss_initial_loadout(seed) for seed in range(1, 25)]
        serialized = {
            (loadout["sword"], tuple(loadout["equipment"]))
            for loadout in loadouts
        }
        self.assertGreater(len(serialized), 1)

        for loadout in loadouts:
            sword = self._enchantments(loadout["sword"])
            bow = self._enchantments(loadout["equipment"][0])
            self.assertEqual(
                len(set(sword) & {"smite", "sharpness"}),
                1,
            )
            self.assertTrue(3 <= sword.get("smite", sword.get("sharpness", 0)) <= 5)
            self.assertTrue(3 <= bow["power"] <= 5)
            self.assertNotIn("protection", sword)
            self.assertNotIn("protection", bow)

            for item in loadout["equipment"][1:]:
                enchantments = self._enchantments(item)
                self.assertTrue(3 <= enchantments["protection"] <= 4)
                if item.startswith("diamond_boots"):
                    self.assertTrue(3 <= enchantments["feather_falling"] <= 4)
                else:
                    self.assertNotIn("feather_falling", enchantments)
                if "unbreaking" in enchantments:
                    self.assertTrue(1 <= enchantments["unbreaking"] <= 3)

            for item in [loadout["sword"], *loadout["equipment"]]:
                enchantments = self._enchantments(item)
                if "unbreaking" in enchantments:
                    self.assertTrue(1 <= enchantments["unbreaking"] <= 3)

    def test_generated_boss_spawn_is_more_than_40_blocks_from_center(self) -> None:
        spec = BossCombatGenerationSpec(
            agent_spawn=(0.0, 0.0, 0.0),
            min_distance=41.0,
            max_distance=50.0,
            y_offset=1.0,
        )

        position = generate_boss_spawn_position(1, spec)

        horizontal = math.hypot(position["x"], position["z"])
        self.assertGreater(horizontal, 40.0)
        self.assertLessEqual(horizontal, 50.0)
        self.assertEqual(position["y"], 1.0)

    def test_generated_boss_spawns_stay_inside_arena(self) -> None:
        spec = BossCombatGenerationSpec(
            agent_spawn=(0.0, 0.0, 0.0),
            min_distance=41.0,
            max_distance=50.0,
            y_offset=1.0,
        )

        for seed in range(100):
            with self.subTest(seed=seed):
                position = generate_boss_spawn_position(seed, spec)
                self.assertTrue(BOSS_SPAWN_MIN_X <= position["x"] <= BOSS_SPAWN_MAX_X)
                self.assertTrue(BOSS_SPAWN_MIN_Y <= position["y"] <= BOSS_SPAWN_MAX_Y)
                self.assertTrue(BOSS_SPAWN_MIN_Z <= position["z"] <= BOSS_SPAWN_MAX_Z)

    def test_generated_agent_spawns_are_inside_center_radius(self) -> None:
        spec = BossCombatGenerationSpec(
            agent_spawn=(5.0, 0.0, -3.0),
            agent_count=4,
            agent_spawn_radius=20.0,
        )

        positions = generate_agent_spawn_positions(1, spec)

        self.assertEqual(len(positions), 4)
        self.assertEqual(len({tuple(position.items()) for position in positions}), 4)
        for position in positions:
            distance = math.hypot(
                position["x"] - spec.agent_spawn[0],
                position["z"] - spec.agent_spawn[2],
            )
            self.assertLessEqual(distance, 20.0)

    def test_generate_task_batch_writes_valid_numbered_config(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            bundles = generate_task_batch(
                ROOT / "src" / "tasks" / "specs" / "boss_combat.yaml",
                count=1,
                base_seed=7,
                output_dir=Path(tmp) / "boss_combat",
                overwrite=False,
            )

            self.assertEqual(len(bundles), 1)
            self.assertEqual(bundles[0].task.name, "7.yaml")
            spec = load_spec(bundles[0].task)
            position = spec.spawns[0]["position"]
            spawn = spec.initial_state["per_agent"]["position"]
            horizontal = math.hypot(position["x"] - spawn["x"], position["z"] - spawn["z"])
            self.assertEqual(spec.task_id, "boss_combat")
            self.assertEqual(spec.agents["count"], 4)
            self.assertGreater(horizontal, 40.0)
            self.assertLessEqual(horizontal, 50.0)
            agent_positions = spec.initial_state["by_agent"]
            self.assertEqual(len(agent_positions), 4)
            for agent in agent_positions:
                agent_distance = math.hypot(
                    agent["position"]["x"] - spawn["x"],
                    agent["position"]["z"] - spawn["z"],
                )
                self.assertLessEqual(agent_distance, 20.0)
            self.assertEqual(spec.params["generation_seed"], 7)


if __name__ == "__main__":
    unittest.main()
