#!/usr/bin/env python3
"""Post-processing text tests for batch equipment results."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action.result_postprocessor import postprocess_action_result  # noqa: E402


def describe(action: str, ok: bool, succeeded, failed) -> str:
    result = postprocess_action_result(
        {
            "action": action,
            "ok": ok,
            "result": {
                "ok": ok,
                "total": len(succeeded) + len(failed),
                "succeeded": succeeded,
                "failed": failed,
                "partial": bool(succeeded) and bool(failed),
                "error_code": None if ok else "all_failed",
            },
        }
    )
    return result["description"]


class EquipmentBatchPostprocessTests(unittest.TestCase):
    def test_equip_all_success(self) -> None:
        text = describe(
            "equipment.equip_items",
            True,
            [
                {"destination": "hand", "item": {"name": "iron_sword"}},
                {"destination": "head", "item": {"name": "diamond_helmet"}},
            ],
            [],
        )
        self.assertEqual(
            text,
            "装备完成：成功装备 2 项（hand:iron_sword、head:diamond_helmet）。",
        )

    def test_equip_partial(self) -> None:
        text = describe(
            "equipment.equip_items",
            True,
            [{"destination": "hand", "item": {"name": "iron_sword"}}],
            [{"destination": "legs", "error_code": "item_missing", "reason": "item_missing"}],
        )
        self.assertEqual(
            text,
            "装备部分成功：成功 1 项（hand:iron_sword）；失败 1 项（legs:item_missing）。",
        )

    def test_equip_all_failed(self) -> None:
        text = describe(
            "equipment.equip_items",
            False,
            [],
            [
                {"destination": "hand", "error_code": "item_missing", "reason": "item_missing"},
                {"destination": None, "error_code": "invalid_item_request", "reason": "invalid_item_request"},
            ],
        )
        self.assertEqual(
            text,
            "装备失败：全部失败（hand:item_missing、invalid_item_request）。",
        )

    def test_unequip_all_success(self) -> None:
        text = describe(
            "equipment.unequip_items",
            True,
            [
                {"destination": "hand", "item": {"name": "iron_sword"}},
                {"destination": "legs", "item": {"name": "iron_leggings"}},
            ],
            [],
        )
        self.assertEqual(
            text,
            "卸下完成：成功卸下 2 个槽位（hand、legs）。",
        )

    def test_unequip_partial(self) -> None:
        text = describe(
            "equipment.unequip_items",
            True,
            [{"destination": "hand", "item": {"name": "iron_sword"}}],
            [{"destination": "head", "error_code": "slot_empty", "reason": "slot_empty"}],
        )
        self.assertEqual(
            text,
            "卸下部分成功：成功 1 项（hand）；失败 1 项（head:slot_empty）。",
        )


if __name__ == "__main__":
    unittest.main()
