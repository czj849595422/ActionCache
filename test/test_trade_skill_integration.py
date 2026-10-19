#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trade Skill 的递归发现、权限、内部调用和 Node 行为测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import BaseActionPlanner, SkillRegistry  # noqa: E402
from src.agents.action.result_postprocessor import (  # noqa: E402
    load_skill_postprocessor,
    postprocess_action_result,
)
from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.tasks import create_default_registry  # noqa: E402


NODE_TEST = ROOT / "test" / "js" / "test_trade_skill.js"
DEMO_PATH = ROOT / "script" / "demo_task_environment.py"


class _FakeBridgeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _post_bridge(self, path, *, json_data, **kwargs):
        self.calls.append((path, json_data))
        return {
            "status": "completed",
            "actionId": json_data["actionId"],
            "action": json_data["action"],
            "mode": "oneshot",
            "result": {"ok": True},
        }


def _load_demo_module():
    spec = importlib.util.spec_from_file_location("_trade_demo_module", DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TradeSkillIntegrationTests(unittest.TestCase):
    def test_node_trade_skill_behaviors(self) -> None:
        result = subprocess.run(
            ["node", str(NODE_TEST)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("trade skill JS tests passed", result.stdout)

    def test_nested_manifest_allowlist_and_duplicate_name(self) -> None:
        registry = SkillRegistry(allowed_actions={"trade.inspect_shop"})
        self.assertIn("trade", registry.manifests)
        self.assertEqual(registry.list_skills()["trade"]["commands"], ["inspect_shop"])
        with self.assertRaises(PermissionError):
            registry.resolve("container.inspect_chest")
        with self.assertRaises(PermissionError):
            registry.resolve("block.break")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first", "nested/second"):
                target = root / name
                target.mkdir(parents=True)
                (target / "skill.json").write_text(
                    json.dumps(
                        {
                            "name": "duplicate",
                            "version": "1",
                            "runtime": "javascript",
                            "commands": {},
                        }
                    ),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(ValueError, "Duplicate skill name"):
                SkillRegistry(root)

    def test_nested_postprocess_and_duplicate_manifest_failure(self) -> None:
        result = postprocess_action_result(
            {
                "action": "trade.inspect_shop",
                "ok": True,
                "result": {
                    "ok": True,
                    "shop_name": "Bob's_shop",
                    "owner": "Bob",
                    "sale_items": [{"name": "minecraft:iron_ingot", "count": 2}],
                },
            }
        )
        self.assertIn("Bob's_shop", result["description"])
        self.assertIn("minecraft:iron_ingot x2", result["description"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "expand/two"):
                target = root / name
                target.mkdir(parents=True)
                (target / "skill.json").write_text(
                    json.dumps({"name": "same", "commands": {}}),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(ValueError, "Duplicate skill name"):
                load_skill_postprocessor("same", skills_dir=root)

    def test_internal_action_bypasses_public_allowlist_only_when_trusted(self) -> None:
        agent = _FakeBridgeAgent()
        planner = BaseActionPlanner(
            agent,
            allowed_actions={"trade.inspect_shop"},
            internal_actions={"container.register_chests"},
        )
        denied = planner.execute("container.register_chests", {})
        self.assertFalse(denied.ok)
        self.assertFalse(agent.calls)

        allowed = planner.execute_internal("container.register_chests", {})
        self.assertTrue(allowed.ok)
        self.assertEqual(agent.calls[0][0], "/skill/execute")
        self.assertTrue(agent.calls[0][1]["internal"])

        untrusted = planner.execute_internal("block.break", {})
        self.assertFalse(untrusted.ok)
        self.assertEqual(len(agent.calls), 1)

    def test_skill_agent_and_demo_inject_trade_scope(self) -> None:
        agent = SkillAgent(
            username="Alice",
            allowed_actions={"trade.inspect_shop"},
            internal_actions={"container.register_chests"},
        )
        self.assertEqual(
            json.loads(agent.bridge_env["SKILL_ACTION_ALLOWLIST"]),
            ["trade.inspect_shop"],
        )
        self.assertEqual(
            json.loads(agent.bridge_env["SKILL_INTERNAL_ACTIONS"]),
            ["container.register_chests"],
        )

        demo = _load_demo_module()
        spec = create_default_registry().get("trade")
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            runtime_path = Path(directory)
            options = demo._skill_agent_options(
                spec,
                runtime_path,
                runtime_path / "chests.json",
            )
            env = options["bridge_env"]
            self.assertTrue(Path(env["TRADE_CONFIG_FILE"]).is_absolute())
            self.assertTrue(Path(env["TRADE_CONFIG_FILE"]).is_file())
            self.assertEqual(Path(env["TRADE_RUNTIME_DIR"]), runtime_path / "trade")
            self.assertEqual(
                options["internal_actions"],
                frozenset({"container.register_chests"}),
            )
            self.assertTrue(all(action.startswith(("trade.", "crafting.", "chat.", "navigation.", "wait.")) for action in options["allowed_actions"]))
            self.assertNotIn("container.inspect_chest", options["allowed_actions"])
            self.assertNotIn("block.break", options["allowed_actions"])
            self.assertNotIn("crafting.craft_item", options["allowed_actions"])
            self.assertIn("trade.take_from_container", options["allowed_actions"])
            self.assertIn("trade.store_to_container", options["allowed_actions"])


if __name__ == "__main__":
    unittest.main()
