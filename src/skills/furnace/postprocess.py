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
    if command == "find_nearby":
        return describe_find_nearby(action_result, payload)
    if command == "open":
        return describe_open(action_result, payload)
    if command == "put_fuel":
        return describe_put(action_result, payload, "燃料槽")
    if command == "take_fuel":
        return describe_take(action_result, payload, "燃料槽")
    if command == "put_input":
        return describe_put(action_result, payload, "原料槽")
    if command == "take_input":
        return describe_take(action_result, payload, "原料槽")
    if command == "take_output":
        return describe_take(action_result, payload, "产物槽")
    if command == "smelt_item":
        return describe_smelt(action_result, payload)
    if command == "close":
        return describe_close(action_result, payload)
    return None


def describe_find_nearby(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"寻找附近熔炉失败：{error_reason(action_result, payload)}。"
    furnaces = payload.get("furnaces") or []
    nearest = format_block(furnaces[0]) if furnaces else "无"
    return f"寻找附近熔炉完成：半径 {payload.get('radius')} 内发现 {len(furnaces)} 个熔炉，最近为 {nearest}。"


def describe_open(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"打开熔炉失败：{error_reason(action_result, payload)}。"
    return (
        f"已打开 {format_block(payload.get('furnace'))}；"
        f"{format_furnace_state(payload.get('furnace_state'))}；"
        f"背包 {format_items(payload.get('inventory_items'))}。"
    )


def describe_put(action_result: dict[str, Any], payload: dict[str, Any], slot_label: str) -> str:
    if not result_ok(action_result, payload):
        if payload.get("missing"):
            return f"放入{slot_label}失败：{format_missing(payload.get('missing'))}。"
        if payload.get("space"):
            return f"放入{slot_label}失败：背包空间不足，详情保留在 raw JSON。"
        return f"放入{slot_label}失败：{error_reason(action_result, payload)}。"
    recovered = payload.get("recovered") or []
    recovered_text = f"；已先回收 {format_items(recovered, count_keys=('recovered', 'count'))}" if recovered else ""
    return (
        f"已向{slot_label}放入 {format_items(payload.get('placed'), count_keys=('placed', 'count'))}"
        f"{recovered_text}；{format_furnace_state(payload.get('furnace_state'))}。"
    )


def describe_take(action_result: dict[str, Any], payload: dict[str, Any], slot_label: str) -> str:
    if not result_ok(action_result, payload):
        if payload.get("missing"):
            return f"取出{slot_label}失败：{format_missing(payload.get('missing'))}。"
        if payload.get("space"):
            return f"取出{slot_label}失败：背包空间不足，详情保留在 raw JSON。"
        return f"取出{slot_label}失败：{error_reason(action_result, payload)}。"
    return (
        f"已从{slot_label}取出 {format_items(payload.get('taken'), count_keys=('taken', 'count'))}；"
        f"{format_furnace_state(payload.get('furnace_state'))}；"
        f"背包 {format_items(payload.get('inventory_items'))}。"
    )


def describe_close(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"关闭熔炉失败：{error_reason(action_result, payload)}。"
    if payload.get("already_closed"):
        return "熔炉窗口此前已关闭。"
    return f"已关闭 {format_block(payload.get('furnace'))}。"


def describe_smelt(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("missing"):
            return f"烧炼失败：{format_missing(payload.get('missing'))}。"
        return f"烧炼失败：{error_reason(action_result, payload)}。"
    item = payload.get("item") or "物品"
    if payload.get("waited"):
        return (
            f"已烧炼 {payload.get('smelted_count', 0)} 个{item}"
            f"（请求 {payload.get('requested_count', 0)} 个），熔炉已关闭。"
        )
    return (
        f"已配置 {payload.get('configured_count', 0)} 个{item}"
        "（未等待烧炼），熔炉已关闭，可执行其他操作。"
    )


def format_furnace_state(state: Any) -> str:
    if not isinstance(state, dict):
        return "熔炉状态未知"
    input_item = format_items([state.get("input")] if state.get("input") else [])
    fuel_item = format_items([state.get("fuel")] if state.get("fuel") else [])
    output_item = format_items([state.get("output")] if state.get("output") else [])
    progress = percent_text(state.get("progress_ratio"))
    fuel = percent_text(state.get("fuel_ratio"))
    return f"熔炉状态：原料 {input_item}，燃料 {fuel_item}，产物 {output_item}，进度 {progress}，燃料剩余 {fuel}"


def percent_text(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{round(value * 100)}%"
    return "未知"
