from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_block,
    format_items,
    format_missing,
    result_ok,
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "inspect_chest":
        return describe_inspect(action_result, payload)
    if command == "pick_object_from_chest":
        return describe_pick(action_result, payload)
    if command == "place_object_to_chest":
        return describe_place(action_result, payload)
    if command == "register_chests":
        return describe_register(action_result, payload)
    if command == "list_chests":
        return describe_list(action_result, payload)
    return None


def describe_inspect(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"检查箱子失败：{error_reason(action_result, payload)}。"
    return (
        f"已打开 {format_block(payload.get('chest'))}：箱内 {format_items(payload.get('chest_items'))}；"
        f"背包 {format_items(payload.get('inventory_items'))}。"
    )


def describe_pick(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("error_code") == "invalid_item_request":
            return f"从箱子取物失败：参数格式错误，{detailed_error_reason(action_result, payload)}。"
        if payload.get("missing"):
            return f"从箱子取物失败：{format_missing(payload.get('missing'))}。"
        if payload.get("space"):
            return f"从箱子取物失败：背包空间不足，详情保留在 raw JSON。"
        return f"从箱子取物失败：{detailed_error_reason(action_result, payload)}。"
    return (
        f"已从箱子取出 {format_items(payload.get('picked'), count_keys=('picked', 'count'))}；"
        f"箱内剩余 {format_items(payload.get('chest_items'))}；背包 {format_items(payload.get('inventory_items'))}。"
    )


def describe_place(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("error_code") == "invalid_item_request":
            return f"向箱子放物失败：参数格式错误，{detailed_error_reason(action_result, payload)}。"
        if payload.get("missing"):
            return f"向箱子放物失败：{format_missing(payload.get('missing'))}。"
        if payload.get("space"):
            return f"向箱子放物失败：箱子空间不足，详情保留在 raw JSON。"
        return f"向箱子放物失败：{detailed_error_reason(action_result, payload)}。"
    return (
        f"已向箱子放入 {format_items(payload.get('placed'), count_keys=('placed', 'count'))}；"
        f"箱内现在 {format_items(payload.get('chest_items'))}；背包 {format_items(payload.get('inventory_items'))}。"
    )


def detailed_error_reason(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    code = payload.get("error_code") or action_result.get("error")
    reason = payload.get("reason")
    if code and reason and str(code) != str(reason):
        return f"{code}：{reason}"
    if reason:
        return str(reason)
    return error_reason(action_result, payload)


def describe_register(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"注册箱子失败：{detailed_error_reason(action_result, payload)}。"
    registered = payload.get("registered") or []
    if not registered:
        return f"没有新箱子需要注册，当前累计 {payload.get('chest_count', 0)} 个。"
    desc = "、".join(
        format_registered_chest(entry)
        for entry in registered[:8]
    )
    if len(registered) > 8:
        desc += f"（另有 {len(registered) - 8} 个）"
    return (
        f"已注册 {payload.get('registered_count', len(registered))} 个箱子"
        f"（累计 {payload.get('chest_count', 0)} 个）：{desc}。"
    )


def describe_list(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"查询箱子注册表失败：{detailed_error_reason(action_result, payload)}。"
    chests = payload.get("chests") or []
    if not chests:
        return "当前没有已注册的箱子，请先执行 container.register_chests。"
    desc = "、".join(
        format_registered_chest(entry)
        for entry in chests[:10]
    )
    if len(chests) > 10:
        desc += f"（另有 {len(chests) - 10} 个）"
    return f"当前已注册 {payload.get('count', len(chests))} 个箱子：{desc}。"


def format_registered_chest(entry: dict[str, Any]) -> str:
    label = f"#{entry.get('id')}"
    if entry.get("name"):
        label += f"({entry.get('name')})"
    return f"{label}@{format_position(entry.get('position'))}"
