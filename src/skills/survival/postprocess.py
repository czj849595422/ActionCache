from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if command != "use_food":
        return None
    payload = result_payload(action_result)
    food = payload.get("food") or payload.get("required_food") or "补给"
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    if not result_ok(action_result, payload):
        return f"使用补给 {food} 失败：{error_reason(action_result, payload)}。"
    restore_text = ""
    if payload.get("restored_held_item") is True:
        current = payload.get("current_held_item") if isinstance(payload.get("current_held_item"), dict) else {}
        restore_text = f"，已恢复手持 {current.get('name') or '原物品'}"
    elif payload.get("restore_reason") and payload.get("restore_reason") != "no_previous_held_item":
        restore_text = f"，未能恢复原手持物：{payload.get('restore_reason')}"
    return (
        f"已使用补给 {food}；生命值 {before.get('health')} -> {after.get('health')}，"
        f"饥饿值 {before.get('food')} -> {after.get('food')}{restore_text}。"
    )
