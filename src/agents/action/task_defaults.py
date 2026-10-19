"""任务级动作默认值（按任务类别在派发前修正动作参数）。"""

from __future__ import annotations

from typing import Any


def apply_task_action_defaults(category: str | None, proposal: Any) -> None:
    """按任务类别修正动作默认值；仅当模型未显式给出参数时覆盖。

    当前规则：清剿任务中 combat.fight 默认 collect_drops=none（不自动拾取），
    除非模型显式提供了 collect_drops。
    """
    if category != "monster_clearance":
        return
    unit = getattr(proposal, "action_unit", None)
    if unit is None or getattr(unit, "action", None) != "combat.fight":
        return
    input_params = getattr(proposal, "input_params", None) or {}
    if "collect_drops" in input_params:
        return
    unit.params["collect_drops"] = "none"


__all__ = ["apply_task_action_defaults"]
