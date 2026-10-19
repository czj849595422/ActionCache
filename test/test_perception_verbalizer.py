"""
test_perception_verbalizer.py - terminal JSON-to-language perception rendering
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.perception.verbalizer import (  # noqa: E402
    EquipmentRule,
    HeldItemRule,
    HostileEntitiesRule,
    InventoryRule,
    NearbyChestsRule,
    SelfStatusRule,
    TeamStatusRule,
    VerbalizerPipeline,
    combat_verbalizer,
    inventory_equipment_verbalizer,
    task1_combat_chest_verbalizer,
)


PREVIEW_PATH = PROJECT_ROOT / ".tmp" / "perception_verbalizer_preview.json"


SAMPLE_OBSERVATION = {
    "self": {
        "health": 18,
        "food": 14,
        "position": {"x": -8.5, "y": -60, "z": 9.5},
        "gameMode": "survival",
        "onGround": True,
        "heldItem": {"name": "stone_sword", "count": 1},
        "inventory": [
            {"name": "cooked_beef", "count": 8},
            {"name": "apple", "count": 3},
        ],
        "equipment": [
            {"name": "leather_helmet", "count": 1},
            {"name": "leather_chestplate", "count": 1},
        ],
    },
    "players": [
        {"username": "self_agent", "isSelf": True},
        {
            "username": "teammate_1",
            "isSelf": False,
            "entity": {
                "position": {"x": -4, "y": -60, "z": 8},
                "distance": 4.7,
            },
        },
    ],
    "entities": [
        {
            "id": 68,
            "uuid": "hostile-1",
            "type": "hostile",
            "name": "zombie",
            "position": {"x": -6.3, "y": -60, "z": 12.2},
            "distance": 3.2,
            "health": 20,
        },
        {
            "id": 2,
            "type": "animal",
            "name": "cow",
            "position": {"x": 2, "y": -60, "z": 2},
            "distance": 12,
        },
    ],
    "items": [
        {
            "entityId": 10,
            "position": {"x": -3, "y": -60, "z": 10},
            "distance": 5.5,
            "item": {"name": "diamond", "count": 3},
        }
    ],
    "blocks": [
        {
            "name": "chest",
            "position": {"x": -2, "y": -60, "z": 11},
            "distance": 3.5,
            "material": "wood",
        },
        {
            "name": "stone",
            "position": {"x": -1, "y": -60, "z": 9},
            "distance": 2.0,
            "material": "rock",
        },
    ],
    "summary": {
        "hostile_count": 1,
        "nearest_hostile_distance": 3.2,
    },
}


class PerceptionVerbalizerTests(unittest.TestCase):
    def test_default_language_is_chinese_and_preserves_rule_order(self):
        verbalizer = VerbalizerPipeline(
            [
                SelfStatusRule(),
                TeamStatusRule(),
                HostileEntitiesRule(),
            ]
        )

        text = verbalizer.render(SAMPLE_OBSERVATION)

        self.assertIn("【自身状态】", text)
        self.assertIn("【队友信息】", text)
        self.assertIn("【敌人信息】", text)
        self.assertLess(text.index("【自身状态】"), text.index("【队友信息】"))
        self.assertLess(text.index("【队友信息】"), text.index("【敌人信息】"))
        self.assertIn("生命值 18", text)
        self.assertIn("zombie(id=68)", text)

    def test_english_language_renders_english_sections(self):
        verbalizer = VerbalizerPipeline(
            [
                SelfStatusRule(),
                HostileEntitiesRule(),
            ],
            language="en",
        )

        text = verbalizer.render(SAMPLE_OBSERVATION)

        self.assertIn("[Self Status]", text)
        self.assertIn("[Hostiles]", text)
        self.assertIn("health 18", text)
        self.assertIn("zombie(id=68)", text)
        self.assertNotIn("【自身状态】", text)

    def test_invalid_language_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "language"):
            VerbalizerPipeline([], language="jp")

    def test_empty_sections_are_skipped(self):
        verbalizer = VerbalizerPipeline(
            [
                TeamStatusRule(),
                HeldItemRule(),
            ]
        )

        text = verbalizer.render({"self": {"heldItem": {"name": "torch", "count": 2}}})

        self.assertNotIn("【队友信息】", text)
        self.assertIn("【手持物品】", text)

    def test_inventory_equipment_preset(self):
        text = inventory_equipment_verbalizer().render(SAMPLE_OBSERVATION)

        self.assertIn("【手持物品】", text)
        self.assertIn("【装备信息】", text)
        self.assertIn("【背包信息】", text)
        self.assertIn("stone_sword x1", text)
        self.assertIn("leather_helmet x1", text)
        self.assertIn("cooked_beef x8", text)

    def test_nearby_chests_rule_only_describes_chests(self):
        text = VerbalizerPipeline([NearbyChestsRule()]).render(SAMPLE_OBSERVATION)

        self.assertIn("【附近箱子】", text)
        self.assertIn("chest", text)
        self.assertNotIn("stone", text)

    def test_task1_combat_chest_preset(self):
        text = task1_combat_chest_verbalizer().render(SAMPLE_OBSERVATION)

        self.assertIn("【自身状态】", text)
        self.assertIn("【敌人信息】", text)
        self.assertIn("【附近箱子】", text)
        self.assertIn("zombie(id=68)", text)

    def test_preview_output_saved_to_tmp(self):
        zh = combat_verbalizer(language="zh").render(SAMPLE_OBSERVATION)
        en = combat_verbalizer(language="en").render(SAMPLE_OBSERVATION)
        payload = {
            "zh": zh,
            "en": en,
            "observation_keys": sorted(SAMPLE_OBSERVATION.keys()),
        }

        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self.assertTrue(PREVIEW_PATH.exists())
        self.assertIn("【自身状态】", zh)
        self.assertIn("[Self Status]", en)


if __name__ == "__main__":
    unittest.main()
