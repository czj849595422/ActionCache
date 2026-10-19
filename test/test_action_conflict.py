"""动作锁切换规则测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action.conflict import ConflictManager  # noqa: E402
from src.agents.action.registry import SkillCommandSpec  # noqa: E402


def spec(*locks: str, switch_group: str | None = None, switchable: bool = True):
    return SkillCommandSpec(
        skill="test",
        command="action",
        locks=set(locks),
        switch_group=switch_group,
        switchable=switchable,
    )


class ConflictManagerTests(unittest.TestCase):
    def test_switchable_actions_can_replace_across_switch_groups(self) -> None:
        manager = ConflictManager()
        manager.acquire("old", "navigation.follow", spec("movement", switch_group="navigation"))

        decision = manager.acquire(
            "new",
            "combat.fight",
            spec("movement", switch_group="combat"),
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.switch_action_id, "old")

    def test_non_switchable_running_action_still_blocks_replacement(self) -> None:
        manager = ConflictManager()
        manager.acquire(
            "old",
            "crafting.craft",
            spec("inventory", switch_group="crafting", switchable=False),
        )

        decision = manager.check(
            "new",
            "combat.fight",
            spec("inventory", switch_group="combat"),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("old", decision.reason or "")


if __name__ == "__main__":
    unittest.main()
