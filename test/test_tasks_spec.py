#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/tasks 任务规格：YAML 解析与校验测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.tasks import TaskSpec, TaskSpecError, load_spec  # noqa: E402


def valid_spec_data() -> dict[str, object]:
    return {
        "task_id": "boss_combat",
        "category": "boss_combat",
        "name": "Wither Combat",
        "agents": {"count": 5},
        "environment": {
            "time": "midnight",
            "weather": "clear",
            "difficulty": "normal",
            "gamemode": "survival",
            "world_spawn": {"x": 0, "y": 64, "z": 0},
            "gamerules": {"keepInventory": False},
            "init_commands": ["say ready"],
        },
        "initial_state": {
            "per_agent": {
                "inventory": {"bow": 1},
                "equipment": ["bow"],
                "position": {"x": 0, "y": 64, "z": 0},
            }
        },
        "spawns": [{"type": "wither", "position": {"x": 8, "y": 65, "z": 8}}],
        "completion": {
            "timeout_seconds": 600,
            "success": {"type": "boss_health_below", "ratio": 0.0},
            "failure": {"type": "all_agents_dead"},
        },
        "params": {"boss_type": "wither"},
    }


def write_yaml(data: dict[str, object]) -> Path:
    directory = tempfile.mkdtemp()
    path = Path(directory) / "task.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


class TaskSpecLoadTests(unittest.TestCase):
    def test_load_valid_yaml(self) -> None:
        path = write_yaml(valid_spec_data())
        spec = load_spec(path)
        self.assertEqual(spec.task_id, "boss_combat")
        self.assertEqual(spec.category, "boss_combat")
        self.assertEqual(spec.agents["count"], 5)
        self.assertEqual(spec.completion["timeout_seconds"], 600)
        self.assertEqual(spec.environment["init_commands"], ["say ready"])
        self.assertEqual(spec.params["boss_type"], "wither")

    def test_missing_required_fields_raise(self) -> None:
        for field_name in ("task_id", "category", "name"):
            data = valid_spec_data()
            del data[field_name]
            with self.assertRaises(TaskSpecError):
                TaskSpec.from_dict(data)

    def test_invalid_agent_count_raises(self) -> None:
        data = valid_spec_data()
        data["agents"] = {"count": 0}
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_invalid_success_type_raises(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {"type": "unknown_type"}
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_invalid_ratio_raises(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {"type": "boss_health_below", "ratio": 1.5}
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_invalid_position_raises(self) -> None:
        data = valid_spec_data()
        data["environment"]["world_spawn"] = {"x": 0, "y": 64}
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_observation_range_defaults_to_20(self) -> None:
        spec = TaskSpec.from_dict(valid_spec_data())
        self.assertEqual(spec.observation_range, 20.0)

    def test_observation_range_from_environment(self) -> None:
        data = valid_spec_data()
        data["environment"]["observation_range"] = 20
        spec = TaskSpec.from_dict(data)
        self.assertEqual(spec.observation_range, 20.0)
        restored = TaskSpec.from_dict(spec.to_dict())
        self.assertEqual(restored.observation_range, 20.0)

    def test_invalid_observation_range_raises(self) -> None:
        for value in (0, -1, "10m"):
            data = valid_spec_data()
            data["environment"]["observation_range"] = value
            with self.assertRaises(TaskSpecError):
                TaskSpec.from_dict(data)

    def test_invalid_timeout_raises(self) -> None:
        data = valid_spec_data()
        data["completion"]["timeout_seconds"] = 0
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_item_counts_requires_items(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {"type": "item_counts"}
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_item_value_requires_positive_target_and_values(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {
            "type": "item_value",
            "target_value": 0,
            "item_values": {"iron_block": 3},
        }
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_all_task_monsters_eliminated_is_valid(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {
            "type": "all_task_monsters_eliminated"
        }
        spec = TaskSpec.from_dict(data)
        self.assertEqual(
            spec.completion["success"]["type"],
            "all_task_monsters_eliminated",
        )

        data["completion"]["success"] = {
            "type": "item_value",
            "target_value": 80,
            "item_values": {"iron_block": -1},
        }
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_all_personal_chests_satisfied_is_valid(self) -> None:
        data = valid_spec_data()
        data["completion"]["success"] = {
            "type": "all_personal_chests_satisfied"
        }
        spec = TaskSpec.from_dict(data)
        self.assertEqual(
            spec.completion["success"]["type"],
            "all_personal_chests_satisfied",
        )

    def test_unknown_top_level_field_raises(self) -> None:
        data = valid_spec_data()
        data["extra"] = 1
        with self.assertRaises(TaskSpecError):
            TaskSpec.from_dict(data)

    def test_invalid_yaml_raises(self) -> None:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "bad.yaml"
        path.write_text("task_id: [unclosed", encoding="utf-8")
        with self.assertRaises(TaskSpecError):
            load_spec(path)

    def test_to_dict_from_dict_roundtrip(self) -> None:
        spec = TaskSpec.from_dict(valid_spec_data())
        restored = TaskSpec.from_dict(spec.to_dict())
        self.assertEqual(restored.task_id, spec.task_id)
        self.assertEqual(restored.completion, spec.completion)
        self.assertEqual(restored.params, spec.params)


if __name__ == "__main__":
    unittest.main()
