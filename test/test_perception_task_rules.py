#!/usr/bin/env python3
"""Task-level world fact composition and text rendering tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.perception import (  # noqa: E402
    TASK_PERCEPTION_PROFILES,
    task_perception,
    task_verbalizer,
)
from src.perception.rule import RuleContext, apply_rule  # noqa: E402
from src.tasks.spec import load_spec  # noqa: E402
from src.tasks.pvp.runner import pvp_spec_with_team_size  # noqa: E402


SPEC_DIR = ROOT / "src" / "tasks" / "specs"


def section(facts):
    return {
        "status": "observed" if facts else "empty",
        "sources": ["test"],
        "freshness": "live",
        "facts": facts,
        "summary": {},
    }


def position(x, y, z):
    return {"x": x, "y": y, "z": z}


def item(name, count):
    return {"name": name, "count": count}


def container(name, pos, distance, contents, *, physical=None, status="observed"):
    return {
        "container_type": "chest",
        "position": pos,
        "physical_positions": physical or [pos],
        "distance": distance,
        "within_use_range": distance <= 4.5,
        "line_of_sight": True,
        "internal_status": status,
        "slots": [dict(value, slot=index) for index, value in enumerate(contents)],
        "totals": contents,
        "empty_slots": 27 - len(contents),
        "total_slots": 27,
        "freshness": "live",
        "test_name": name,
    }


def sample_world():
    obstacles = [
        {
            "block_name": "stone",
            "position": position(index % 4, 0, index // 4),
            "distance": 1 + index * 0.1,
            "bounding_box": "block",
            "transparent": False,
            "diggable": True,
            "properties": {},
        }
        for index in range(16)
    ]
    obstacles.extend(
        [
            {
                "block_name": "stone",
                "position": position(0, -1, 0),
                "distance": 1,
                "bounding_box": "block",
                "transparent": False,
            },
            {
                "block_name": "crafting_table",
                "position": position(1, 0, 0),
                "distance": 1,
                "bounding_box": "block",
                "transparent": False,
            },
        ]
    )
    return {
        "schema_version": "world_perception.v1",
        "sections": {
            "self_state": section([{"agent_name": "Alice", "position": position(0, 0, 0)}]),
            "held_and_equipment": section([{"main_hand": item("iron_pickaxe", 1)}]),
            "inventory": section(
                [
                    {
                        "totals": [item("iron_block", 2), item("coal", 4), item("arrow", 8)],
                        "slots": [item("iron_block", 2), item("coal", 4), item("arrow", 8)],
                    }
                ]
            ),
            "players": section(
                [
                    {"player_name": "Bob", "distance": 2},
                    {"player_name": "visitor", "distance": 1},
                ]
            ),
            "hostile_entities": section(
                [
                    {"entity_id": 1, "kind": "creeper", "distance": 1, "roles": []},
                    {"entity_id": 2, "kind": "wither", "distance": 8, "roles": ["boss"]},
                    {"entity_id": 3, "kind": "zombie", "distance": 3, "roles": []},
                    {"entity_id": 4, "kind": "skeleton", "distance": 4, "roles": []},
                ]
            ),
            "projectiles_explosives": section(
                [{"entity_id": 5, "kind": "arrow", "distance": 1}]
            ),
            "dropped_items": section(
                [
                    {"item": item("iron_block", 1), "distance": 2},
                    {"item": item("arrow", 8), "distance": 3},
                    {"item": item("splash_potion", 1), "distance": 4},
                    {"item": item("coal", 1), "distance": 1},
                ]
            ),
            "nearby_blocks": section(obstacles),
            "resources": section(
                [
                    {"resource_name": "iron_block", "nearest_distance": 2},
                    {"resource_name": "coal_ore", "nearest_distance": 1},
                ]
            ),
            "containers": section(
                [
                    container("own", position(1, 0, 6), 2, [item("iron_ingot", 8)]),
                    container("private", position(-1, 0, -6), 3, [item("diamond", 6)]),
                    container(
                        "shop",
                        position(4, 0, 0),
                        4,
                        [item("arrow", 8), item("gold_nugget", 12)],
                        physical=[position(4, 0, -1), position(4, 0, 0)],
                    ),
                    container("unregistered", position(9, 0, 9), 3, [item("netherite_ingot", 1)]),
                ]
            ),
            "workstations": section(
                [
                    {"workstation_type": "crafting_table", "position": position(1, 0, 0), "distance": 1},
                    {"workstation_type": "furnace", "position": position(2, 0, 0), "distance": 2},
                ]
            ),
        },
    }


class TaskPerceptionRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = {
            "boss_combat": load_spec(SPEC_DIR / "boss_combat.yaml"),
            "mining": load_spec(SPEC_DIR / "mining.yaml"),
            "monster_clearance": load_spec(SPEC_DIR / "monster_clearance.yaml"),
            "pvp": pvp_spec_with_team_size(load_spec(SPEC_DIR / "pvp.yaml"), 2),
            "trade": load_spec(SPEC_DIR / "trade.yaml"),
        }

    def apply(self, category, *, metrics=None, registry_path=None):
        return apply_rule(
            task_perception(
                self.specs[category],
                metrics=metrics or {},
                elapsed_seconds=12.5,
                registry_path=registry_path,
            ),
            sample_world(),
            RuleContext(
                mode="global",
                sub_mode="radius",
                radius=16,
                agent_name="Alice",
            ),
        )

    def test_profiles_use_confirmed_sections_and_remove_w07(self):
        boss = self.apply("boss_combat")
        clearance = self.apply("monster_clearance")
        self.assertEqual(
            tuple(boss["sections"]),
            TASK_PERCEPTION_PROFILES["boss_combat"].sections,
        )
        self.assertNotIn("projectiles_explosives", boss["sections"])
        self.assertNotIn("projectiles_explosives", clearance["sections"])
        self.assertIn("containers", TASK_PERCEPTION_PROFILES["trade"].sections)
        self.assertIn("players", TASK_PERCEPTION_PROFILES["pvp"].sections)

    def test_boss_prioritizes_boss_and_bounds_obstacle_facts(self):
        snapshot = self.apply("boss_combat")
        hostiles = snapshot["sections"]["hostile_entities"]["facts"]
        self.assertEqual(hostiles[0]["kind"], "wither")
        # 清剿与 Boss 观测不再注入【附近方块】。
        self.assertNotIn("nearby_blocks", snapshot["sections"])
        self.assertNotIn("nearby_blocks", TASK_PERCEPTION_PROFILES["boss_combat"].sections)
        self.assertNotIn(
            "nearby_blocks",
            TASK_PERCEPTION_PROFILES["monster_clearance"].sections,
        )

    def test_injected_boss_keeps_uuid_separate_from_entity_id(self):
        world = sample_world()
        world["sections"]["hostile_entities"]["facts"] = [
            fact
            for fact in world["sections"]["hostile_entities"]["facts"]
            if fact["kind"] != "wither"
        ]
        snapshot = apply_rule(
            task_perception(
                self.specs["boss_combat"],
                metrics={
                    "boss_uuid": "73479e72-1111-2222-3333-444444444444",
                    "boss_position": position(8, 0, 0),
                },
                elapsed_seconds=12.5,
            ),
            world,
            RuleContext(
                mode="global",
                sub_mode="radius",
                radius=16,
                agent_name="Alice",
            ),
        )

        boss = next(
            fact
            for fact in snapshot["sections"]["hostile_entities"]["facts"]
            if fact.get("kind") == "wither"
        )
        self.assertIn("entity_id", boss)
        self.assertEqual(boss["entity_id"], "boss")
        self.assertEqual(boss["uuid"], "73479e72-1111-2222-3333-444444444444")
        text = task_verbalizer("boss_combat").render(snapshot)
        self.assertIn("wither_boss", text)
        self.assertNotIn("wither_73479e72", text)

    def test_clearance_task_progress_new_format(self):
        snapshot = self.apply(
            "monster_clearance",
            metrics={
                "eliminated_monsters": 3,
                "alive_agents": ["Alice", "Bob", "Carol", "Dave"],
            },
        )
        text = task_verbalizer("monster_clearance").render(snapshot)
        self.assertIn("【任务进度】", text)
        self.assertIn("时间：12.5/300；已消灭：3；存活：Alice、Bob、Carol、Dave", text)
        self.assertNotIn("已用时", text)

    def test_boss_task_progress_new_format(self):
        snapshot = self.apply(
            "boss_combat",
            metrics={
                "current_health": 150,
                "max_health": 300,
                "alive_agents": ["Alice", "Bob"],
            },
        )
        text = task_verbalizer("boss_combat").render(snapshot)
        self.assertIn("时间：12.5/600；Boss血量：150/300；存活：Alice、Bob。", text)
        self.assertNotIn("已用时", text)

    def test_mining_uses_manager_ore_groups_instead_of_raw_resources(self):
        snapshot = self.apply(
            "mining",
            metrics={
                "current_value": 6,
                "target_value": 80,
                "active_groups": 2,
                "active_ore_groups": [
                    {
                        "group_id": "g1",
                        "block": "minecraft:iron_block",
                        "positions": [
                            position(6, 0, 0),
                            position(7, 0, 0),
                            position(10, 0, 0),
                        ],
                    },
                    {
                        "group_id": "g2",
                        "block": "minecraft:gold_block",
                        "positions": [position(2, 0, 0)],
                    },
                ],
            },
        )
        drops = snapshot["sections"]["dropped_items"]["facts"]
        inventory = snapshot["sections"]["inventory"]["facts"][0]["totals"]
        self.assertNotIn("resources", snapshot["sections"])
        self.assertEqual([value["item"]["name"] for value in drops], ["iron_block"])
        self.assertEqual([value["name"] for value in inventory], ["iron_block"])
        self.assertEqual(snapshot["task"]["facts"][0]["current_value"], 6)
        self.assertNotIn("ore_groups", snapshot["task"]["facts"][0])
        groups = [
            fact
            for fact in snapshot["sections"]["nearby_blocks"]["facts"]
            if fact.get("is_ore_group")
        ]
        self.assertEqual([group["block_count"] for group in groups], [1, 2, 1])
        self.assertEqual(groups[0]["resource_name"], "gold_block")
        self.assertEqual(groups[0]["anchor_position"], position(2, 0, 0))
        text = task_verbalizer("mining").render(snapshot)
        self.assertIn("gold_block 矿物组（1个方块）位于 (2,0,0)，距离 2.6 格", text)
        self.assertIn("iron_block 矿物组（2个方块）位于 (6,0,0)，距离 6.5 格", text)
        self.assertIn("iron_block 矿物组（1个方块）位于 (10,0,0)，距离 10.5 格", text)
        self.assertIn("时间：12.5/300；收集进度：6/80。", text)

    def test_clearance_keeps_zombies_skeletons_and_task_supplies(self):
        snapshot = self.apply("monster_clearance")
        hostiles = snapshot["sections"]["hostile_entities"]["facts"]
        drops = snapshot["sections"]["dropped_items"]["facts"]
        self.assertEqual({value["kind"] for value in hostiles}, {"zombie", "skeleton"})
        self.assertEqual(
            {value["item"]["name"] for value in drops},
            {"arrow", "splash_potion"},
        )

    def test_clearance_keeps_armor_and_bread_in_inventory_and_drops(self):
        world = sample_world()
        world["sections"]["inventory"] = section(
            [
                {
                    "totals": [
                        item("stone_sword", 1),
                        item("iron_chestplate", 1),
                        item("iron_leggings", 1),
                        item("bread", 2),
                        item("dirt", 1),
                    ],
                    "slots": [
                        item("stone_sword", 1),
                        item("iron_chestplate", 1),
                        item("iron_leggings", 1),
                        item("bread", 2),
                        item("dirt", 1),
                    ],
                }
            ]
        )
        world["sections"]["dropped_items"] = section(
            [
                {"item": item("iron_boots", 1), "distance": 2},
                {"item": item("bread", 2), "distance": 3},
                {"item": item("dirt", 1), "distance": 1},
            ]
        )
        snapshot = apply_rule(
            task_perception(
                self.specs["monster_clearance"],
                metrics={},
                elapsed_seconds=1.0,
            ),
            world,
            RuleContext(
                mode="global",
                sub_mode="radius",
                radius=16,
                agent_name="Alice",
            ),
        )
        inventory = snapshot["sections"]["inventory"]["facts"][0]["totals"]
        self.assertEqual(
            {value["name"] for value in inventory},
            {"stone_sword", "iron_chestplate", "iron_leggings", "bread"},
        )
        drops = snapshot["sections"]["dropped_items"]["facts"]
        self.assertEqual(
            {value["item"]["name"] for value in drops},
            {"iron_boots", "bread"},
        )

    def test_pvp_marks_friend_enemy_and_filters_supplies(self):
        world = sample_world()
        world["sections"]["players"] = section(
            [
                {"player_name": "Blake", "distance": 2},
                {"player_name": "Eli", "distance": 5},
                {"player_name": "visitor", "distance": 1},
            ]
        )
        world["sections"]["inventory"] = section(
            [
                {
                    "totals": [
                        item("stone_sword", 1),
                        item("potion", 1),
                        item("dirt", 1),
                    ],
                    "slots": [
                        item("stone_sword", 1),
                        item("potion", 1),
                        item("dirt", 1),
                    ],
                }
            ]
        )
        world["sections"]["dropped_items"] = section(
            [
                {"item": item("arrow", 8), "distance": 2},
                {"item": item("potion", 1), "distance": 3},
                {"item": item("splash_potion", 1), "distance": 4},
            ]
        )
        metrics = {
            "teams": {
                "alpha": {"agents": ["Aria", "Blake"], "alive_count": 2},
                "bravo": {"agents": ["Eli", "Faye"], "alive_count": 2},
            },
            "spawned_supplies": 3,
            "gear_probability": 0.5,
            "drop_class_probabilities": {"gear": 0.5, "ammo": 0.2, "food": 0.2, "healing": 0.1},
            "food_healing_disabled": False,
            "bounds": {"min_x": -50, "max_x": 50, "min_z": -50, "max_z": 50},
            "out_of_bounds_interval_seconds": 10,
            "out_of_bounds_damage": 1,
            "hunger_pressure_after_seconds": 300,
            "hunger_interval_seconds": 10,
        }
        snapshot = apply_rule(
            task_perception(
                self.specs["pvp"],
                metrics=metrics,
                elapsed_seconds=150.0,
            ),
            world,
            RuleContext(
                mode="global",
                sub_mode="radius",
                radius=16,
                agent_name="Aria",
            ),
        )
        players = {
            fact["player_name"]: fact for fact in snapshot["sections"]["players"]["facts"]
        }
        self.assertEqual(players["Blake"]["relation"], "friendly")
        self.assertEqual(players["Eli"]["relation"], "enemy")
        self.assertNotIn("visitor", players)
        inventory = snapshot["sections"]["inventory"]["facts"][0]["totals"]
        self.assertEqual({value["name"] for value in inventory}, {"stone_sword", "potion"})
        drops = snapshot["sections"]["dropped_items"]["facts"]
        self.assertEqual({value["item"]["name"] for value in drops}, {"arrow", "potion"})
        text = task_verbalizer("pvp").render(snapshot)
        self.assertIn("队友：\nBlake：", text)
        self.assertIn("敌方：\nEli：", text)
        self.assertNotIn("splash_potion", text)
        self.assertNotIn("装备概率", text)
        self.assertNotIn("出界每", text)

    def test_trade_uses_registry_names_and_enforces_container_visibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "chests.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "chests": [
                            {"id": 1, "name": "Alice's_box", "position": position(1, 0, 6)},
                            {"id": 2, "name": "Bob's_box", "position": position(-1, 0, -6)},
                            {"id": 3, "name": "Carol's_shop", "position": position(4, 0, -1)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            metrics = {
                "agents": {
                    "Alice": {
                        "targets": {
                            "shield": {
                                "required": 1,
                                "current": 0,
                                "missing": 1,
                                "unit_price": 12,
                            }
                        },
                        "target_total_value": 12,
                        "complete": False,
                    }
                },
                "completed_agents": 0,
                "total_agents": 4,
                "query_errors": {},
            }
            snapshot = self.apply("trade", metrics=metrics, registry_path=registry_path)

        containers = {
            value["registered_name"]: value
            for value in snapshot["sections"]["containers"]["facts"]
        }
        self.assertEqual(
            set(containers),
            {"Alice's_box", "Bob's_box", "Carol's_shop"},
        )
        self.assertEqual(containers["Alice's_box"]["internal_status"], "observed")
        self.assertEqual(containers["Bob's_box"]["internal_status"], "access_denied")
        self.assertEqual(containers["Bob's_box"]["totals"], [])
        self.assertEqual(
            [value["name"] for value in containers["Carol's_shop"]["sale_totals"]],
            ["arrow"],
        )
        self.assertEqual(
            snapshot["sections"]["workstations"]["facts"][0]["workstation_type"],
            "crafting_table",
        )
        text = task_verbalizer("trade").render(snapshot)
        self.assertIn("crafting_table：位置 (1,0,0)，距离 1.0 格。", text)
        self.assertIn("Carol's_shop", text)
        self.assertIn("arrow×8", text)
        self.assertNotIn("gold_nugget", text)
        self.assertIn("shield 0/1，缺少 1", text)
        self.assertNotIn("单价", text)
        self.assertNotIn("minecraft:shield", text)


if __name__ == "__main__":
    unittest.main()
