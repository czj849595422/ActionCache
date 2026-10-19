from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, format_entity, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if command != "lock_nearest_hostile":
        return None
    payload = result_payload(action_result)
    if not result_ok(action_result, payload):
        return f"demo 战斗锁定失败：{error_reason(action_result, payload)}。"
    target = format_entity(payload.get("target"))
    count = payload.get("attack_count", 0)
    return f"demo 战斗正在锁定最近敌人 {target}，已攻击 {count} 次。"
