#!/usr/bin/env python3
"""W01-W16 world fact generation and read-only block entity tests."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.perception import (  # noqa: E402
    RconBlockEntityProvider,
    WORLD_SECTION_NAMES,
    WorldRuleOptions,
    select_world_sections,
    world_snapshot,
)
from src.perception.rule import RuleContext, apply_rule  # noqa: E402
from src.utils.snbt import parse_first_snbt_compound, parse_snbt  # noqa: E402


def item(name: str, count: int, slot: int, nbt=None):
    return {
        "name": name,
        "count": count,
        "slot": slot,
        "stackSize": 64,
        "nbt": nbt,
    }


def block(name, x, y, z, distance, *, properties=None, transparent=False):
    return {
        "name": name,
        "position": {"x": x, "y": y, "z": z},
        "distance": distance,
        "transparent": transparent,
        "boundingBox": "block",
        "diggable": True,
        "harvestToolNames": ["stone_pickaxe"],
        "properties": properties or {},
        "light": 7,
        "skyLight": 15,
        "biome": {"name": "plains"},
    }


class MappingBlockEntityProvider:
    def __init__(self):
        self.calls = []

    def read_block_entity(self, position, block_name):
        key = (position["x"], position["y"], position["z"])
        self.calls.append((key, block_name))
        payloads = {
            (2, 0, 0): {
                "Items": [{"Slot": 0, "id": "minecraft:iron_ingot", "Count": 4}]
            },
            (3, 0, 0): {
                "Items": [{"Slot": 1, "id": "minecraft:stick", "Count": 8}]
            },
            (0, 0, 2): {
                "Items": [
                    {"Slot": 0, "id": "minecraft:iron_ore", "Count": 3},
                    {"Slot": 1, "id": "minecraft:coal", "Count": 1},
                    {"Slot": 2, "id": "minecraft:iron_ingot", "Count": 2},
                ],
                "BurnTime": 100,
                "CookTime": 50,
                "CookTimeTotal": 100,
            },
        }
        return {
            "nbt": payloads[key],
            "freshness": "live",
            "observed_at_ms": 1234,
            "age_ms": 0,
        }


def sample_raw():
    zombie = {
        "id": 10,
        "uuid": "zombie-10",
        "type": "hostile",
        "name": "zombie",
        "position": {"x": 0, "y": 0, "z": 6},
        "distance": 6,
        "velocity": {"x": 0, "y": 0, "z": 0},
        "equipmentSlots": {},
    }
    cow = {
        "id": 11,
        "type": "animal",
        "name": "cow",
        "position": {"x": -2, "y": 0, "z": 4},
        "distance": 4.5,
        "velocity": {"x": 0, "y": 0, "z": 0},
    }
    enderman = {
        "id": 14,
        "type": "hostile",
        "kind": "Hostile mobs",
        "name": "enderman",
        "position": {"x": 3, "y": 0, "z": 4},
        "distance": 5,
        "velocity": {"x": 0, "y": 0, "z": 0},
    }
    arrow = {
        "id": 12,
        "type": "projectile",
        "name": "arrow",
        "position": {"x": 1, "y": 1, "z": 3},
        "distance": 3.3,
        "velocity": {"x": -0.1, "y": 0, "z": -0.2},
    }
    boat = {
        "id": 13,
        "type": "object",
        "name": "boat",
        "position": {"x": -3, "y": 0, "z": 0},
        "distance": 3,
        "velocity": {"x": 0, "y": 0, "z": 0},
    }
    player = {
        "id": 20,
        "type": "player",
        "username": "Bob",
        "name": "player",
        "position": {"x": 1, "y": 0, "z": -2},
        "distance": 2.3,
        "velocity": {"x": 0, "y": 0, "z": 0},
        "equipmentSlots": {},
    }
    blocks = [
        block("chest", 2, 0, 0, 2, properties={"type": "left", "facing": "north"}),
        block("chest", 3, 0, 0, 3, properties={"type": "right", "facing": "north"}),
        block("chest", 5, 0, 0, 5, properties={"type": "single", "facing": "north"}),
        block("chest", 6, 0, 0, 6, properties={"type": "single", "facing": "north"}),
        block("furnace", 0, 0, 2, 2, properties={"lit": True}),
        block("crafting_table", 1, 0, 1, 1.5),
        block("iron_block", 8, 0, 0, 8),
        block("iron_block", 9, 0, 0, 9),
    ]
    return {
        "self": {
            "username": "Alice",
            "position": {"x": 0, "y": 0, "z": 0},
            "velocity": {"x": 0, "y": 0, "z": 0},
            "yaw": 0,
            "pitch": 0,
            "health": 20,
            "maxHealth": 20,
            "attackEventSequence": 3,
            "lastAttackAt": 1234.5,
            "food": 18,
            "maxFood": 20,
            "oxygenLevel": 20,
            "maxOxygenLevel": 20,
            "onGround": True,
            "heldItem": item("iron_sword", 1, 36),
            "equipmentSlots": {"mainHand": item("iron_sword", 1, 36)},
            "inventory": [
                item("splash_potion", 1, 9, {"Potion": "minecraft:healing"}),
                item("splash_potion", 1, 10, {"Potion": "minecraft:strong_healing"}),
            ],
            "effects": {},
        },
        "entities": [zombie, cow, enderman, arrow, boat, player],
        "players": [
            {"username": "Alice", "isSelf": True, "entity": {}},
            {"username": "Bob", "isSelf": False, "entity": player},
            {"username": "tab-only", "isSelf": False, "entity": None},
        ],
        "items": [
            {
                "entityId": 30,
                "uuid": "drop-30",
                "item": item("arrow", 8, -1),
                "position": {"x": 2, "y": 0, "z": 2},
                "distance": 2.8,
                "velocity": {"x": 0, "y": 0, "z": 0},
            }
        ],
        "blocks": blocks,
        "environment": {
            "dimension": "overworld",
            "difficulty": "normal",
            "time": {"timeOfDay": 18000, "day": 3},
            "weather": {"isRaining": False, "rainState": 0, "thunderState": 0},
        },
        "metadata": {
            "mode": "local",
            "subMode": "radius",
            "radius": 16,
            "blockRadius": 10,
            "fovDegrees": 90,
            "timestamp": 123456789,
            "counts": {
                "entities": 6,
                "players": 3,
                "items": 1,
                "blocks": len(blocks),
                "scannedBlocks": 9261,
                "airBlocks": 9000,
                "unloadedBlocks": 0,
            },
        },
    }


class WorldRuleTests(unittest.TestCase):
    def setUp(self):
        self.raw = sample_raw()
        self.provider = MappingBlockEntityProvider()
        self.context = RuleContext(
            mode="local",
            sub_mode="radius",
            radius=16,
            agent_name="Alice",
        )

    def snapshot(self):
        return apply_rule(
            world_snapshot(provider=self.provider), self.raw, self.context
        )

    def test_generates_all_world_sections_and_classifies_entities(self):
        snapshot = self.snapshot()
        self.assertEqual(tuple(snapshot["sections"]), WORLD_SECTION_NAMES)
        self.assertEqual(snapshot["sections"]["players"]["facts"][0]["player_name"], "Bob")
        self.assertEqual(
            snapshot["sections"]["self_state"]["facts"][0]["attack_event_sequence"],
            3,
        )
        self.assertEqual(
            snapshot["sections"]["self_state"]["facts"][0]["last_attack_at"],
            1234.5,
        )
        self.assertEqual(len(snapshot["sections"]["players"]["facts"]), 1)
        self.assertEqual(snapshot["sections"]["hostile_entities"]["facts"][0]["kind"], "zombie")
        creatures = snapshot["sections"]["passive_neutral_entities"]["facts"]
        self.assertEqual({value["kind"] for value in creatures}, {"cow", "enderman"})
        enderman = next(value for value in creatures if value["kind"] == "enderman")
        self.assertEqual(enderman["entity_class"], "neutral")
        self.assertNotIn(
            "enderman",
            {value["kind"] for value in snapshot["sections"]["hostile_entities"]["facts"]},
        )
        self.assertEqual(snapshot["sections"]["projectiles_explosives"]["facts"][0]["kind"], "arrow")
        self.assertEqual(snapshot["sections"]["other_entities"]["facts"][0]["kind"], "boat")
        self.assertTrue(snapshot["sections"]["projectiles_explosives"]["facts"][0]["moving_toward_self"])

    def test_inventory_distinguishes_same_item_with_different_nbt(self):
        totals = self.snapshot()["sections"]["inventory"]["facts"][0]["totals"]
        self.assertEqual(len(totals), 2)
        self.assertNotEqual(totals[0]["nbt_fingerprint"], totals[1]["nbt_fingerprint"])

    def test_registry_fields_survive_normalization_and_keep_unique_totals(self):
        self.raw["self"]["inventory"] = [
            {
                **item("diamond_sword", 1, 9),
                "identifier": "diamond_sword_1",
                "owner": "Alice",
                "enchantments": [{"id": "sharpness", "level": 5}],
            },
            {
                **item("diamond_sword", 1, 10),
                "identifier": "diamond_sword_2",
                "owner": "Alice",
                "enchantments": [],
            },
        ]
        stacks = self.snapshot()["sections"]["inventory"]["facts"][0]["stacks"]
        totals = self.snapshot()["sections"]["inventory"]["facts"][0]["totals"]
        self.assertEqual({stack["identifier"] for stack in stacks}, {"diamond_sword_1", "diamond_sword_2"})
        self.assertEqual(len(totals), 2)
        self.assertEqual(stacks[0]["owner"], "Alice")
        self.assertEqual(stacks[0]["enchantments"][0]["id"], "sharpness")

    def test_double_chest_merges_only_matching_halves_and_preserves_slots(self):
        containers = self.snapshot()["sections"]["containers"]["facts"]
        self.assertEqual(len(containers), 3)
        double = next(value for value in containers if len(value["physical_positions"]) == 2)
        self.assertEqual(double["internal_status"], "observed")
        self.assertEqual(double["total_slots"], 54)
        self.assertEqual([value["slot"] for value in double["slots"]], [0, 28])
        self.assertEqual(double["empty_slots"], 52)
        singles = [value for value in containers if len(value["physical_positions"]) == 1]
        self.assertEqual(len(singles), 2)
        self.assertTrue(all(value["internal_status"] == "out_of_range" for value in singles))

    def test_processor_and_resource_facts_use_server_data_and_connected_groups(self):
        snapshot = self.snapshot()["sections"]
        furnace = snapshot["processors"]["facts"][0]
        self.assertEqual(furnace["internal_status"], "observed")
        self.assertEqual(furnace["input"]["name"], "iron_ore")
        self.assertEqual(furnace["progress_ratio"], 0.5)
        iron = next(
            value for value in snapshot["resources"]["facts"]
            if value["resource_name"] == "iron_block"
        )
        self.assertEqual(iron["block_count"], 2)
        self.assertEqual(iron["harvest_tools"], ["stone_pickaxe"])

    def test_enhancement_can_be_disabled_without_provider_calls(self):
        snapshot = apply_rule(
            world_snapshot(
                options=WorldRuleOptions(
                    container_internal_enabled=False,
                    processor_internal_enabled=False,
                ),
                provider=self.provider,
            ),
            self.raw,
            self.context,
        )
        self.assertFalse(self.provider.calls)
        self.assertEqual(
            snapshot["sections"]["processors"]["facts"][0]["internal_status"],
            "disabled",
        )

    def test_top_level_selector_keeps_only_requested_sections(self):
        selected = apply_rule(
            select_world_sections("self_state", "resources"),
            self.snapshot(),
            self.context,
        )
        self.assertEqual(list(selected["sections"]), ["self_state", "resources"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            select_world_sections("danger")


class FakeRconServer:
    def __init__(self):
        self.commands = []
        self.fail = False

    def send_command(self, command):
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("rcon unavailable")
        return (
            "Block data: {Items:[{Slot:0b,id:\"minecraft:iron_ingot\",Count:4b}],"
            "Custom:[I;1,2,3]}"
        )


class SnbtAndProviderTests(unittest.TestCase):
    def test_snbt_supports_nested_compounds_lists_and_typed_arrays(self):
        value = parse_snbt(
            '{Items:[{Slot:0b,id:"minecraft:potion",tag:{Potion:"minecraft:healing"}}],A:[I;1,2,3]}'
        )
        self.assertEqual(value["Items"][0]["Slot"], 0)
        self.assertEqual(value["Items"][0]["tag"]["Potion"], "minecraft:healing")
        self.assertEqual(value["A"], [1, 2, 3])
        self.assertEqual(
            parse_first_snbt_compound("prefix " + str('{x:1b}') + " suffix"),
            {"x": 1},
        )

    def test_provider_uses_short_cache_and_stale_fallback_on_query_error(self):
        monotonic = [10.0]
        wall = [100.0]
        server = FakeRconServer()
        provider = RconBlockEntityProvider(
            server,
            cache_ttl_ms=500,
            stale_if_error_ms=5000,
            clock=lambda: monotonic[0],
            wall_clock=lambda: wall[0],
        )
        position = {"x": 1, "y": 2, "z": 3}
        first = provider.read_block_entity(position, "chest")
        self.assertEqual(first["freshness"], "live")
        monotonic[0] = 10.2
        cached = provider.read_block_entity(position, "chest")
        self.assertEqual(cached["freshness"], "cached")
        self.assertEqual(len(server.commands), 1)
        monotonic[0] = 11.0
        server.fail = True
        fallback = provider.read_block_entity(position, "chest")
        self.assertEqual(fallback["freshness"], "cached")
        self.assertIn("rcon unavailable", fallback["query_error"])
        self.assertEqual(len(server.commands), 2)


if __name__ == "__main__":
    unittest.main()
