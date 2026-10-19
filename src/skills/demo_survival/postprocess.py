from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if command != "flee_nearest_hostile":
        return None
    payload = result_payload(action_result)
    if not result_ok(action_result, payload):
        return f"demo 逃离敌人失败：{error_reason(action_result, payload)}。"
    safe_distance = payload.get("safeDistance") or payload.get("safe_distance") or "目标"
    return f"demo 逃跑动作正在执行，正在远离附近敌人，目标安全距离为 {safe_distance}。"
