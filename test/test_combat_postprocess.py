#!/usr/bin/env python3
"""Post-processing text tests for the combat skill results."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action.result_postprocessor import postprocess_action_result  # noqa: E402


class CombatObservePostprocessTests(unittest.TestCase):
    def test_fight_result_reports_target_state(self) -> None:
        dead = postprocess_action_result(
            {
                "action": "combat.fight",
                "ok": True,
                "result": {
                    "ok": True,
                    "target_dead": True,
                    "weapon": "stone_sword",
                    "target": {"name": "zombie", "id": 8},
                    "attack_count": 3,
                },
            }
        )
        alive = postprocess_action_result(
            {
                "action": "combat.fight",
                "ok": True,
                "result": {
                    "ok": True,
                    "target_dead": False,
                    "timeout": True,
                    "weapon": "stone_sword",
                    "target": {"name": "zombie", "id": 8},
                },
            }
        )
        self.assertIn("zombie(id=8)已被消灭", dead["description"])
        self.assertIn("zombie(id=8)依然存活", alive["description"])

    def test_observe_success_with_hostiles_and_items(self) -> None:
        result = postprocess_action_result(
            {
                "action": "combat.observe",
                "ok": True,
                "result": {
                    "ok": True,
                    "radius": 20,
                    "agents": [
                        {
                            "username": "Alex",
                            "name": "player",
                            "id": 7,
                            "distance": 2.4,
                            "position": {"x": 2, "y": 0, "z": 1},
                        }
                    ],
                    "hostiles": [
                        {
                            "name": "zombie",
                            "id": 65,
                            "distance": 3.2,
                            "position": {"x": 3, "y": 0, "z": 0},
                        }
                    ],
                    "items": [
                        {
                            "name": "arrow",
                            "count": 8,
                            "distance": 5.0,
                            "position": {"x": 5, "y": 0, "z": 0},
                        }
                    ],
                },
            }
        )
        self.assertEqual(
            result["description"],
            "侦查完成：2倍感知半径（20 格）内发现 1 个agent"
            "：Alex_7(2,0,1)，距离 2.4；发现 1 个敌对实体"
            "：zombie_65(3,0,0)，距离 3.2；发现 1 个掉落物：arrow×8(5,0,0)。",
        )

    def test_boss_result_and_observe_labels_are_compact(self) -> None:
        observed = postprocess_action_result(
            {
                "action": "combat.observe",
                "ok": True,
                "result": {
                    "ok": True,
                    "radius": 20,
                    "agents": [],
                    "hostiles": [
                        {
                            "name": "wither",
                            "id": 70,
                            "uuid": "73479e72-1111-2222-3333-444444444444",
                            "position": {"x": 3, "y": 0, "z": 0},
                        }
                    ],
                    "items": [],
                },
            }
        )
        fought = postprocess_action_result(
            {
                "action": "combat.fight",
                "ok": True,
                "result": {
                    "ok": True,
                    "target_dead": True,
                    "weapon": "stone_sword",
                    "target": {
                        "name": "wither",
                        "id": 70,
                        "uuid": "73479e72-1111-2222-3333-444444444444",
                    },
                    "attack_count": 3,
                },
            }
        )

        self.assertIn("wither_70(3,0,0)", observed["description"])
        self.assertNotIn("wither_73479e72", observed["description"])
        self.assertIn("wither_70已被消灭", fought["description"])
        self.assertNotIn("73479e72", fought["description"])

    def test_observe_success_with_empty_results(self) -> None:
        result = postprocess_action_result(
            {
                "action": "combat.observe",
                "ok": True,
                "result": {
                    "ok": True,
                    "radius": 20,
                    "agents": [],
                    "hostiles": [],
                    "items": [],
                },
            }
        )
        self.assertEqual(
            result["description"],
            "侦查完成：2倍感知半径（20 格）内未发现agent；未发现敌对实体；未发现掉落物。",
        )

    def test_observe_failure(self) -> None:
        result = postprocess_action_result(
            {
                "action": "combat.observe",
                "ok": False,
                "result": {"ok": False, "error_code": "perception_failed"},
                "error": "perception_failed",
            }
        )
        self.assertEqual(result["description"], "侦查失败：perception_failed。")

    def test_use_food_failure_uses_required_food_and_nested_result(self) -> None:
        result = postprocess_action_result(
            {
                "action": "combat.use_food",
                "ok": True,
                "result": {
                    "ok": False,
                    "required_food": "potion[healing]",
                    "error_code": "missing_food",
                },
            }
        )
        self.assertEqual(
            result["description"],
            "使用补给失败（potion[healing]）：missing_food。",
        )


if __name__ == "__main__":
    unittest.main()
