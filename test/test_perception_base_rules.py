"""
test_perception_base_rules.py - reusable perception base rules
"""

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.perception.rule import (
    ChainRule,
    ParallelRule,
    RuleContext,
    blocks_by_name_contains,
    blocks_with_property,
    drop_fields,
    entities_by_type,
    filter_by_contains,
    filter_by_range,
    in_view,
    items_by_count,
    items_by_name_contains,
    nearest_blocks,
    nearest_entities,
    nearest_items,
    pick_fields,
    rename_fields,
    select_path,
    sort_by,
    top_k,
    within_box,
    within_distance,
)


RAW = {
    "self": {
        "position": {"x": 0, "y": 64, "z": 0},
        "yaw": 0,
        "pitch": 0,
        "health": 20,
        "food": 18,
    },
    "entities": [
        {"id": 1, "type": "hostile", "name": "zombie", "position": {"x": 0, "y": 64, "z": -5}, "distance": 5, "health": 20},
        {"id": 2, "type": "animal", "name": "cow", "position": {"x": 3, "y": 64, "z": 0}, "distance": 3, "health": 10},
        {"id": 3, "type": "hostile", "name": "skeleton", "position": {"x": 0, "y": 64, "z": 8}, "distance": 8, "health": 20},
    ],
    "items": [
        {"entityId": 10, "distance": 4, "position": {"x": 4, "y": 64, "z": 0}, "item": {"name": "diamond", "count": 3}},
        {"entityId": 11, "distance": 6, "position": {"x": 6, "y": 64, "z": 0}, "item": {"name": "rotten_flesh", "count": 2}},
    ],
    "blocks": [
        {"name": "stone", "material": "mineable/pickaxe", "distance": 1, "position": {"x": 1, "y": 64, "z": 0}, "properties": {}},
        {"name": "oak_chest", "material": "wood", "distance": 3, "position": {"x": 3, "y": 64, "z": 0}, "properties": {"facing": "north"}},
    ],
}


def context() -> RuleContext:
    return RuleContext(
        mode="global",
        sub_mode="radius",
        radius=None,
        metadata={"raw_data": RAW},
    )


class PerceptionBaseRuleTests(unittest.TestCase):
    def test_select_and_pick_fields_preserve_path(self):
        selected = select_path("self.position")(RAW, context())
        picked = pick_fields("entities", ["id", "name"])(RAW, context())

        self.assertEqual(selected, {"self": {"position": {"x": 0, "y": 64, "z": 0}}})
        self.assertEqual(picked["entities"][0], {"id": 1, "name": "zombie"})

    def test_drop_and_rename_fields(self):
        dropped = drop_fields("entities", ["health"])(RAW, context())
        renamed = rename_fields("self", {"health": "hp"})(RAW, context())

        self.assertNotIn("health", dropped["entities"][0])
        self.assertEqual(renamed["self"]["hp"], 20)
        self.assertNotIn("health", renamed["self"])

    def test_generic_filters(self):
        by_contains = filter_by_contains("entities", "name", "zom")(RAW, context())
        by_range = filter_by_range("entities", "distance", max_value=5)(RAW, context())

        self.assertEqual([e["name"] for e in by_contains["entities"]], ["zombie"])
        self.assertEqual([e["id"] for e in by_range["entities"]], [1, 2])

    def test_sort_and_top_k(self):
        sorted_result = sort_by("entities", "distance")(RAW, context())
        top_result = top_k("entities", 2, key="distance")(RAW, context())

        self.assertEqual([e["id"] for e in sorted_result["entities"]], [2, 1, 3])
        self.assertEqual([e["id"] for e in top_result["entities"]], [2, 1])

    def test_spatial_rules(self):
        near = within_distance("entities", 5)(RAW, context())
        boxed = within_box("entities", half_size=(4, 2, 6))(RAW, context())
        viewed = in_view("entities", fov_degrees=90)(RAW, context())

        self.assertEqual([e["id"] for e in near["entities"]], [1, 2])
        self.assertEqual([e["id"] for e in boxed["entities"]], [1, 2])
        self.assertEqual([e["id"] for e in viewed["entities"]], [1])

    def test_entity_item_block_wrappers(self):
        hostiles = entities_by_type("hostile")(RAW, context())
        diamonds = items_by_name_contains("diamond")(RAW, context())
        item_count = items_by_count(min_count=3)(RAW, context())
        chests = blocks_by_name_contains("chest")(RAW, context())
        facing_blocks = blocks_with_property("facing")(RAW, context())

        self.assertEqual([e["id"] for e in hostiles["entities"]], [1, 3])
        self.assertEqual([i["entityId"] for i in diamonds["items"]], [10])
        self.assertEqual([i["entityId"] for i in item_count["items"]], [10])
        self.assertEqual([b["name"] for b in chests["blocks"]], ["oak_chest"])
        self.assertEqual([b["name"] for b in facing_blocks["blocks"]], ["oak_chest"])

    def test_nearest_wrappers(self):
        self.assertEqual([e["id"] for e in nearest_entities(2)(RAW, context())["entities"]], [2, 1])
        self.assertEqual([i["entityId"] for i in nearest_items(1)(RAW, context())["items"]], [10])
        self.assertEqual([b["name"] for b in nearest_blocks(1)(RAW, context())["blocks"]], ["stone"])

    def test_chain_and_parallel_composition(self):
        rule = ParallelRule(
            {
                "self": pick_fields("self", ["health", "position"]),
                "hostiles": ChainRule(
                    [
                        entities_by_type("hostile"),
                        top_k("entities", 1, key="distance"),
                        pick_fields("entities", ["id", "name", "distance"]),
                    ]
                ),
                "items": nearest_items(1),
            }
        )

        result = rule.apply(RAW, context())

        self.assertEqual(result["self"], {"health": 20, "position": {"x": 0, "y": 64, "z": 0}})
        self.assertEqual(result["entities"], [{"id": 1, "name": "zombie", "distance": 5}])
        self.assertEqual(result["items"][0]["entityId"], 10)


if __name__ == "__main__":
    unittest.main()
