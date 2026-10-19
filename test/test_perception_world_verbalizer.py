#!/usr/bin/env python3
"""Terminal rendering tests for W01-W16 world fact sections."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.perception import WORLD_SECTION_NAMES, world_verbalizer  # noqa: E402


def section(status="observed", facts=None, **extra):
    return {
        "status": status,
        "sources": ["mineflayer_raw"],
        "freshness": "live",
        "facts": facts or [],
        "summary": {},
        **extra,
    }


class WorldVerbalizerTests(unittest.TestCase):
    def test_self_state_health_renders_current_over_max(self):
        snapshot = {
            "schema_version": "world_perception.v1",
            "sections": {
                "self_state": section(
                    facts=[
                        {
                            "agent_name": "Alice",
                            "health": 17.8,
                            "max_health": 20,
                            "health_delta": -2.5,
                            "recent_attack_count": 2,
                            "attack_event_sequence": 2,
                            "last_attack_at": 1234.5,
                            "food": 20,
                            "max_food": 20,
                            "position": {"x": 1, "y": 0, "z": 2},
                            "on_ground": True,
                        }
                    ]
                )
            },
        }
        text = world_verbalizer(sections=["self_state"]).render(snapshot)
        self.assertIn("Alice：生命值 17.8（减少 2.5）/20", text)
        self.assertIn("近期受到攻击次数：2次", text)
        self.assertLess(
            text.index("位置 (1.0,0.0,2.0)"),
            text.index("近期受到攻击次数：2次"),
        )
        self.assertNotIn("attack_event_sequence", text)
        self.assertNotIn("last_attack_at", text)

    def test_registry_equipment_uses_identifier_and_enchantments(self):
        snapshot = {
            "schema_version": "world_perception.v1",
            "sections": {
                "held_and_equipment": section(
                    facts=[{
                        "main_hand": {
                            "name": "diamond_sword",
                            "identifier": "diamond_sword_1",
                            "enchantments": [{"id": "sharpness", "level": 5}],
                            "durability": 1500,
                            "max_durability": 1561,
                        },
                        "off_hand": None,
                        "head": None,
                        "chest": None,
                        "legs": None,
                        "feet": None,
                    }]
                )
            },
        }
        text = world_verbalizer(sections=["held_and_equipment"]).render(snapshot)
        self.assertIn("diamond_sword_1 (sharpness 5)", text)

    def test_renders_statuses_cache_and_truncation_without_mutating_snapshot(self):
        snapshot = {
            "schema_version": "world_perception.v1",
            "sections": {
                "hostile_entities": section(
                    facts=[
                        {
                            "kind": "zombie",
                            "entity_id": 1,
                            "position": {"x": 0, "y": 0, "z": 4},
                            "distance": 4,
                            "relative_direction": "front",
                        },
                        {
                            "kind": "skeleton",
                            "entity_id": 2,
                            "position": {"x": 2, "y": 0, "z": 5},
                            "distance": 5.4,
                        },
                    ]
                ),
                "containers": section(
                    facts=[
                        {
                            "container_type": "barrel",
                            "position": {"x": 2, "y": 0, "z": 1},
                            "distance": 2.2,
                            "internal_status": "observed",
                            "totals": [{"name": "iron_ingot", "count": 4}],
                            "empty_slots": 26,
                            "total_slots": 27,
                            "freshness": "cached",
                            "age_ms": 2300,
                        }
                    ]
                ),
                "processors": section(
                    status="unknown", reason="RCON unavailable"
                ),
                "workstations": section(status="empty"),
            },
        }
        original = repr(snapshot)
        text = world_verbalizer(
            sections=["hostile_entities", "containers", "processors", "workstations"],
            limits={"hostile_entities": 1},
        ).render(snapshot)
        self.assertIn("【敌对实体】", text)
        self.assertIn("zombie_1", text)
        self.assertIn("\u3010\u5bb9\u5668\u6216\u5546\u5e97\u3011", text)
        self.assertNotIn("\u3010\u5bb9\u5668\u3011", text)
        self.assertIn("另有 1 个对象未展开", text)
        self.assertIn("内容来自 2.3 秒前的缓存", text)
        self.assertIn("感知失败，当前状态未知", text)
        self.assertIn("范围内未发现工作站", text)
        self.assertEqual(repr(snapshot), original)

    def test_boss_label_is_compact_even_when_fact_has_internal_ids(self):
        snapshot = {
            "schema_version": "world_perception.v1",
            "sections": {
                "hostile_entities": section(
                    facts=[
                        {
                            "kind": "wither",
                            "entity_id": 70,
                            "uuid": "73479e72-1111-2222-3333-444444444444",
                            "roles": ["boss"],
                            "position": {"x": 0, "y": 0, "z": 4},
                        }
                    ]
                )
            },
        }

        text = world_verbalizer(sections=["hostile_entities"]).render(snapshot)

        self.assertIn("wither", text)
        self.assertIn("wither_70", text)
        self.assertNotIn("wither_boss", text)
        self.assertNotIn("73479e72", text)

    def test_disabled_sections_are_hidden_by_default(self):
        snapshot = {"sections": {"containers": section(status="disabled")}}
        self.assertEqual(
            world_verbalizer(sections=["containers"]).render(snapshot), ""
        )
        self.assertIn(
            "增强感知未启用",
            world_verbalizer(
                sections=["containers"], show_disabled=True
            ).render(snapshot),
        )

    def test_all_world_sections_have_titles_and_english_is_supported(self):
        sections = {
            name: section(status="empty") for name in WORLD_SECTION_NAMES
        }
        zh = world_verbalizer().render({"sections": sections})
        en = world_verbalizer(language="en").render({"sections": sections})
        self.assertIn("【自身】", zh)
        self.assertIn("【感知范围】", zh)
        self.assertIn("[Self]", en)
        self.assertIn("[Observation Coverage]", en)

    def test_invalid_section_and_limit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            world_verbalizer(sections=["danger"])
        with self.assertRaisesRegex(ValueError, "non-negative"):
            world_verbalizer(limits={"players": -1})


if __name__ == "__main__":
    unittest.main()
