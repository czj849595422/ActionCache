#!/usr/bin/env python3
"""Observer account filtering tests for perception."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.debug.task_adapter import TaskDebugAdapter  # noqa: E402
from src.perception import (  # noqa: E402
    BasePerception,
    filter_observer_players,
    world_snapshot,
)
from src.perception.rule import RuleContext, apply_rule  # noqa: E402


def _entity(entity_id, username, distance):
    return {
        "id": entity_id,
        "uuid": f"entity-{entity_id}",
        "type": "player",
        "name": "player",
        "username": username,
        "position": {"x": distance, "y": 0, "z": 0},
        "distance": distance,
        "velocity": {"x": 0, "y": 0, "z": 0},
        "equipmentSlots": {},
    }


def sample_raw():
    bob = _entity(20, "Bob", 2)
    observer = _entity(30, "observer01", 3)
    observer_without_username = _entity(31, None, 4)
    zombie = {
        "id": 40,
        "uuid": "zombie-40",
        "type": "hostile",
        "name": "zombie",
        "position": {"x": 0, "y": 0, "z": 6},
        "distance": 6,
        "velocity": {"x": 0, "y": 0, "z": 0},
    }
    return {
        "self": {
            "username": "Alice",
            "position": {"x": 0, "y": 0, "z": 0},
            "yaw": 0,
            "pitch": 0,
            "inventory": [],
            "equipmentSlots": {},
        },
        "players": [
            {"username": "Alice", "isSelf": True, "entity": {}},
            {"username": "Bob", "isSelf": False, "entity": bob},
            {"username": "observer01", "isSelf": False, "entity": observer},
            {
                "username": "ObserverCam",
                "isSelf": False,
                "entity": observer_without_username,
            },
        ],
        "entities": [bob, observer, observer_without_username, zombie],
        "items": [],
        "blocks": [],
        "metadata": {
            "mode": "local",
            "subMode": "radius",
            "radius": 16,
            "counts": {
                "players": 4,
                "entities": 4,
                "items": 0,
                "blocks": 0,
            },
        },
    }


def player_names(raw):
    return [player["username"] for player in raw.get("players", [])]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.trust_env = True

    def get(self, *args, **kwargs):
        return FakeResponse(sample_raw())


class FakeDebugAgent:
    username = "Alice"

    def get_raw_perception(self, **kwargs):
        return sample_raw()


class ObserverPerceptionFilterTests(unittest.TestCase):
    def test_filter_removes_observer_players_and_matching_entities(self):
        raw = sample_raw()

        filtered = filter_observer_players(raw)

        self.assertEqual(player_names(filtered), ["Alice", "Bob"])
        self.assertEqual(
            [entity.get("username") for entity in filtered["entities"]],
            ["Bob", None],
        )
        self.assertEqual(filtered["entities"][1]["name"], "zombie")
        self.assertEqual(filtered["metadata"]["counts"]["players"], 2)
        self.assertEqual(filtered["metadata"]["counts"]["entities"], 2)
        self.assertEqual(len(raw["players"]), 4)
        self.assertEqual(raw["metadata"]["counts"]["players"], 4)
        self.assertNotIn("observer", json.dumps(filtered, ensure_ascii=False).lower())

    def test_filter_removes_spectator_players_and_matching_entities(self):
        raw = sample_raw()
        spectator = _entity(50, "Dax", 5)
        raw["players"].append(
            {
                "username": "Dax",
                "isSelf": False,
                "gamemode": "spectator",
                "entity": spectator,
            }
        )
        raw["entities"].append(spectator)
        raw["metadata"]["counts"]["players"] = 5
        raw["metadata"]["counts"]["entities"] = 5

        filtered = filter_observer_players(raw)

        self.assertEqual(player_names(filtered), ["Alice", "Bob"])
        self.assertNotIn("Dax", json.dumps(filtered, ensure_ascii=False))
        self.assertEqual(filtered["metadata"]["counts"]["players"], 2)
        self.assertEqual(filtered["metadata"]["counts"]["entities"], 2)

    def test_base_perception_filters_collected_raw_data(self):
        with patch(
            "src.perception.base_perception.requests.Session",
            return_value=FakeSession(),
        ):
            perception = BasePerception(bridge_url="http://bridge")
            filtered = perception.perceive()

        self.assertEqual(player_names(filtered), ["Alice", "Bob"])
        self.assertNotIn("observer", json.dumps(filtered, ensure_ascii=False).lower())

    def test_fallback_session_ignores_environment_proxy(self):
        with patch(
            "src.perception.base_perception.requests.Session",
            return_value=FakeSession(),
        ) as session_factory:
            BasePerception(bridge_url="http://bridge").perceive()

        self.assertFalse(session_factory.return_value.trust_env)

    def test_world_snapshot_defensively_filters_unfiltered_raw_data(self):
        snapshot = apply_rule(
            world_snapshot(),
            sample_raw(),
            RuleContext(
                mode="local",
                sub_mode="radius",
                radius=16,
                agent_name="Alice",
            ),
        )

        facts = snapshot["sections"]["players"]["facts"]
        self.assertEqual([fact["player_name"] for fact in facts], ["Bob"])
        self.assertNotIn("observer", json.dumps(snapshot, ensure_ascii=False).lower())

    def test_debug_raw_observation_uses_same_filter(self):
        adapter = object.__new__(TaskDebugAdapter)
        adapter._agents = {"Alice": FakeDebugAgent()}

        filtered = adapter.observe_agent("Alice", {"representation": "raw"})

        self.assertEqual(player_names(filtered), ["Alice", "Bob"])
        self.assertNotIn("observer", json.dumps(filtered, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
