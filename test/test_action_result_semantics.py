"""Regression tests for bridge result status normalization."""

from __future__ import annotations

import unittest

from src.agents.action.result_postprocessor import result_ok
from src.agents.action.schemas import ActionResult, ActionStatus


class ActionResultSemanticsTests(unittest.TestCase):
    def test_nested_failure_overrides_outer_success(self) -> None:
        data = {
            "status": "completed",
            "actionId": "a1",
            "result": {"ok": False, "error_code": "missing_food"},
        }
        result = ActionResult.from_bridge("combat.use_food", data)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertEqual(result.error, "missing_food")
        self.assertFalse(result_ok({"ok": True, "result": data["result"]}))

    def test_nested_success_is_accepted_when_outer_has_no_status(self) -> None:
        result = ActionResult.from_bridge(
            "combat.use_food",
            {"result": {"ok": True, "consumed": True}},
        )
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
