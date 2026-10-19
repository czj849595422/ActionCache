#!/usr/bin/env python3
"""Post-processing text tests for the inventory skill results."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action.result_postprocessor import postprocess_action_result  # noqa: E402


class InventoryPickPostprocessTests(unittest.TestCase):
    def test_pick_success_with_picked_list(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.pick",
                "ok": True,
                "result": {
                    "ok": True,
                    "status": "completed",
                    "total_positions": 2,
                    "completed_positions": 2,
                    "pending_positions": [],
                    "picked_items": [
                        {"name": "arrow", "count": 8},
                        {"name": "iron_ingot", "count": 2},
                    ],
                    "picked_total": 10,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "拾取完成：共拾取 10 个物品（arrow×8、iron_ingot×2）；已覆盖 2/2 个目标位置。",
        )

    def test_pick_interrupted_with_pending_positions(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.pick",
                "ok": True,
                "result": {
                    "ok": True,
                    "stopped": True,
                    "status": "stopped",
                    "total_positions": 3,
                    "completed_positions": 1,
                    "pending_positions": [{"x": 5, "y": 0, "z": 5}],
                    "picked_items": [{"name": "arrow", "count": 8}],
                    "picked_total": 8,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "拾取中断：已拾取 8 个物品（arrow×8）；剩余 1 个目标位置。",
        )

    def test_pick_success_with_empty_list(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.pick",
                "ok": True,
                "result": {
                    "ok": True,
                    "status": "completed",
                    "total_positions": 1,
                    "completed_positions": 1,
                    "pending_positions": [],
                    "picked_items": [],
                    "picked_total": 0,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "拾取完成：共拾取 0 个物品（无）；已覆盖 1/1 个目标位置。",
        )

    def test_pick_failure(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.pick",
                "ok": False,
                "result": {
                    "ok": False,
                    "error_code": "invalid_params",
                    "reason": "positions must be a non-empty array",
                },
                "error": "invalid_params",
            }
        )
        self.assertEqual(result["description"], "拾取失败：invalid_params。")

    def test_drop_success(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.drop",
                "ok": True,
                "result": {
                    "ok": True,
                    "status": "completed",
                    "position": {"x": 1.5, "y": 0.0, "z": 2.5},
                    "dropped_items": [
                        {"name": "stone_sword", "count": 1},
                        {"name": "bread", "count": 2},
                    ],
                    "dropped_total": 3,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "在 (1.5, 0.0, 2.5) 丢弃了 stone_sword×1、bread×2，共 3 件物品。",
        )

    def test_drop_failure_missing_item(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.drop",
                "ok": False,
                "result": {
                    "ok": False,
                    "error_code": "missing_item:iron_chestplate",
                    "name": "iron_chestplate",
                },
                "error": "missing_item",
            }
        )
        self.assertEqual(
            result["description"],
            "丢弃失败：missing_item:iron_chestplate。",
        )

    def test_give_completed(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.give",
                "ok": True,
                "result": {
                    "ok": True,
                    "status": "completed",
                    "target": "Alice",
                    "position": {"x": 3.0, "y": 0.0, "z": 4.0},
                    "given_items": [{"name": "iron_chestplate", "count": 1}],
                    "given_total": 1,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "在 (3.0, 0.0, 4.0) 将 iron_chestplate×1 交给了 Alice，请对方及时拾取。",
        )

    def test_give_interrupted(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.give",
                "ok": True,
                "result": {
                    "ok": True,
                    "stopped": True,
                    "status": "stopped",
                    "target": "Alice",
                    "position": {"x": 3.0, "y": 0.0, "z": 4.0},
                    "given_items": [{"name": "bread", "count": 1}],
                    "given_total": 1,
                },
            }
        )
        self.assertEqual(
            result["description"],
            "给予中断：已交给 Alice bread×1，请对方及时拾取。",
        )

    def test_give_failure_target_not_found(self) -> None:
        result = postprocess_action_result(
            {
                "action": "inventory.give",
                "ok": False,
                "result": {
                    "ok": False,
                    "error_code": "target_not_found",
                    "target": "Nobody",
                },
                "error": "target_not_found",
            }
        )
        self.assertEqual(result["description"], "给予失败：target_not_found。")


if __name__ == "__main__":
    unittest.main()
