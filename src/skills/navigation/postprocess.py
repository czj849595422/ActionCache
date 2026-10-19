from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_entity,
    format_position,
    result_ok,
    result_payload,
)


def _error_text(error_code: str, payload: dict[str, Any]) -> str:
    if error_code == "entity_not_found":
        return "未找到指定实体。"
    if error_code == "entity_not_in_range":
        return f"目标实体不在允许范围内（最大 {payload.get('max_range')} 格）。"
    if error_code == "entity_lost":
        return "跟随目标实体丢失。"
    if error_code == "entity_out_of_range":
        return f"目标实体超出允许范围（最大 {payload.get('max_range')} 格）。"
    if error_code == "block_name_unknown":
        return f"未知方块名：{payload.get('block_name')}。"
    if error_code == "block_not_found":
        return f"在 {payload.get('searched_distance')} 格内未找到方块 {payload.get('block_name')}。"
    if error_code == "timeout":
        return "导航超时。"
    if error_code == "invalid_params":
        return "参数无效。"
    return error_code


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "goto_position":
        if not result_ok(action_result, payload):
            return f"导航失败：{_error_text(error_reason(action_result, payload), payload)}"
        status = "已到达目标附近" if payload.get("reached", True) else "导航动作完成但未确认到达"
        target = payload.get("target")
        if isinstance(target, dict) and target.get("name"):
            target_text = f"，目标实体为 {format_entity(target)}"
        elif isinstance(target, dict) and target.get("x") is not None:
            target_text = f"，目标位置为 {format_position(target)}"
        else:
            target_text = ""
        return f"{status}{target_text}，当前位置为 {format_position(payload.get('position'))}。"
    if command == "follow_position":
        if not result_ok(action_result, payload):
            return f"跟随坐标失败：{_error_text(error_reason(action_result, payload), payload)}。"
        state = "正在跟随坐标" if payload.get("moving", True) else "跟随坐标动作已更新"
        return f"{state}，当前位置为 {format_position(payload.get('position'))}。"
    if command == "follow_entity":
        if not result_ok(action_result, payload):
            return f"跟随实体失败：{_error_text(error_reason(action_result, payload), payload)}"
        reason = payload.get("reason")
        if reason == "entity_lost":
            return "跟随实体丢失，动作已结束。"
        if reason == "entity_out_of_range":
            return f"目标实体超出允许范围（最大 {payload.get('max_range')} 格），动作已结束。"
        state = "正在跟随实体" if payload.get("moving", True) else "跟随实体动作已更新"
        target = payload.get("target")
        target_text = format_entity(target) if isinstance(target, dict) else "未知"
        return f"{state}，目标 {target_text}，当前位置为 {format_position(payload.get('position'))}。"
    if command == "goto_block":
        if not result_ok(action_result, payload):
            return f"寻找方块失败：{_error_text(error_reason(action_result, payload), payload)}"
        block = payload.get("block") or {}
        name = block.get("name") or block.get("displayName") or "方块"
        status = "已到达方块附近" if payload.get("reached", True) else "已找到方块但未确认到达"
        return (
            f"{status}：{name} @ {format_position(block.get('position'))}，"
            f"当前位置为 {format_position(payload.get('position'))}。"
        )
    if command == "explore":
        return describe_explore(action_result, payload)
    return None


def describe_explore(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("error_code") == "target_not_found":
            return (
                f"探索未找到目标（{payload.get('reason')}）："
                f"已移动 {payload.get('blocks_moved', 0)} 段，访问 {payload.get('visited_count', 0)} 处。"
            )
        return f"探索失败：{error_reason(action_result, payload)}。"

    reason_text = {
        "target_found": "已找到目标",
        "duration_reached": "达到探索时限",
        "max_distance_reached": "达到最大探索距离",
        "stuck": "探索受阻（已尝试换向仍无法前进）",
    }.get(payload.get("reason"), payload.get("reason") or "未知")
    parts = [
        f"探索完成（{reason_text}）：移动 {payload.get('blocks_moved', 0)} 段，"
        f"访问 {payload.get('visited_count', 0)} 处"
    ]
    found = payload.get("found") or []
    discoveries = payload.get("discoveries") or {}
    if found:
        desc = "、".join(
            f"{block.get('name')}@{format_position(block.get('position'))}"
            for block in found[:8]
        )
        parts.append(f"发现目标 {desc}")
    elif discoveries:
        sorted_disc = sorted(
            discoveries.items(),
            key=lambda pair: (-int(pair[1] or 0), str(pair[0])),
        )
        disc_text = "、".join(
            f"{name} x{count}" for name, count in sorted_disc[:8]
        )
        parts.append(f"沿途发现 {disc_text}")
    return "；".join(parts) + "。"
