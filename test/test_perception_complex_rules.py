"""
test_perception_complex_rules.py - task-oriented perception rule groups
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.perception.rule import RuleContext
from src.perception.rule.complex import (
    combat_snapshot,
    environment_counts,
    navigation_snapshot,
    nearby_overview,
    pickup_snapshot,
    threat_summary,
    valuable_pickups,
)


RAW_PATH = PROJECT_ROOT / "docs" / "perception_raw_test_2.json"
PREVIEW_PATH = PROJECT_ROOT / ".tmp" / "perception_complex_rules_preview.json"


SAMPLE_RAW = {
    "self": {
        "health": 20,
        "food": 18,
        "position": {"x": 0, "y": 64, "z": 0},
        "yaw": 0,
        "pitch": 0,
        "velocity": {"x": 0, "y": 0, "z": 0},
        "onGround": True,
        "gameMode": "survival",
        "effects": [{"name": "night_vision", "duration": 120}],
    },
    "entities": [
        {"id": 1, "type": "hostile", "name": "zombie", "position": {"x": 0, "y": 64, "z": -5}, "distance": 5, "health": 20},
        {"id": 2, "type": "animal", "name": "cow", "position": {"x": 3, "y": 64, "z": 0}, "distance": 3, "health": 10},
    ],
    "items": [
        {"entityId": 10, "position": {"x": 4, "y": 64, "z": 0}, "distance": 4, "item": {"name": "diamond", "displayName": "Diamond", "count": 3}},
        {"entityId": 11, "position": {"x": 6, "y": 64, "z": 0}, "distance": 6, "item": {"name": "rotten_flesh", "displayName": "Rotten Flesh", "count": 2}},
    ],
    "blocks": [
        {"name": "stone", "position": {"x": 1, "y": 64, "z": 0}, "distance": 1, "boundingBox": "block", "diggable": True, "material": "mineable/pickaxe", "transparent": False},
        {"name": "glass", "position": {"x": 2, "y": 64, "z": 0}, "distance": 2, "boundingBox": "block", "diggable": True, "material": "glass", "transparent": True},
    ],
}


def load_raw() -> dict:
    if RAW_PATH.exists():
        return json.loads(RAW_PATH.read_text(encoding="utf-8"))
    return SAMPLE_RAW


def context(raw: dict) -> RuleContext:
    return RuleContext(
        mode="global",
        sub_mode="radius",
        radius=None,
        metadata={"raw_data": raw},
    )


class PerceptionComplexRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = load_raw()
        cls.context = context(cls.raw)

    def test_combat_snapshot_returns_self_and_hostiles(self):
        result = combat_snapshot(k=5, radius=32).apply(self.raw, self.context)

        self.assertIn("self", result)
        self.assertEqual(result["self"], self.raw["self"])
        self.assertIn("entities", result)
        self.assertTrue(all(entity.get("type") == "hostile" for entity in result["entities"]))
        self.assertLessEqual(len(result["entities"]), 5)

    def test_pickup_rules_return_dropped_items(self):
        result = pickup_snapshot(k=5, radius=32).apply(self.raw, self.context)
        valuable = valuable_pickups(names=["diamond"], k=5, radius=32).apply(self.raw, self.context)

        self.assertIn("items", result)
        self.assertEqual(result["self"], self.raw["self"])
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertTrue(all("item" in item for item in result["items"]))
        self.assertTrue(any(item["item"]["name"] == "diamond" for item in valuable["items"]))

    def test_navigation_snapshot_returns_non_transparent_blocks(self):
        result = navigation_snapshot(block_k=10, radius=32).apply(self.raw, self.context)

        self.assertIn("self", result)
        self.assertEqual(result["self"], self.raw["self"])
        self.assertIn("blocks", result)
        self.assertLessEqual(len(result["blocks"]), 10)
        self.assertTrue(all(not block.get("transparent", False) for block in result["blocks"]))

    def test_environment_counts_returns_summary(self):
        result = environment_counts(top_n=10).apply(self.raw, self.context)

        self.assertIn("summary", result)
        self.assertIn("entity_type_counts", result["summary"])
        self.assertIn("block_name_counts", result["summary"])
        self.assertIn("item_name_counts", result["summary"])

    def test_threat_summary_returns_counts(self):
        result = threat_summary(k=10, radius=32).apply(self.raw, self.context)

        self.assertIn("summary", result)
        self.assertIn("hostile_count", result["summary"])
        self.assertIn("nearest_hostile_distance", result["summary"])

    def test_nearby_overview_is_json_serializable_and_saved_to_tmp(self):
        preview = {
            "combat": combat_snapshot(k=5, radius=32).apply(self.raw, self.context),
            "pickup": pickup_snapshot(k=5, radius=32).apply(self.raw, self.context),
            "navigation": navigation_snapshot(block_k=10, radius=32).apply(self.raw, self.context),
            "environment": environment_counts(top_n=10).apply(self.raw, self.context),
            "overview": nearby_overview(entity_k=5, block_k=10, item_k=5).apply(self.raw, self.context),
        }

        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")

        self.assertTrue(PREVIEW_PATH.exists())
        self.assertIn("summary", preview["overview"])
        self.assertEqual(preview["overview"]["self"], self.raw["self"])


if __name__ == "__main__":
    unittest.main()
