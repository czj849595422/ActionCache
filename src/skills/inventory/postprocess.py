from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_equipment,
    format_items,
    format_item,
    item_name,
    result_ok,
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "pick":
        return describe_pick(action_result, payload)
    if command == "drop":
        return describe_drop(action_result, payload)
    if command == "give":
        return describe_give(action_result, payload)
    if not result_ok(action_result, payload):
        return f"{command} 执行失败：{error_reason(action_result, payload)}。"
    if command in {"list", "inspect"}:
        items = payload.get("inventory_items") or payload.get("items") or []
        empty_slots = payload.get("inventory_empty_slots")
        held = item_name(payload.get("heldItem")) if payload.get("heldItem") else "空"
        empty_text = f"空槽 {empty_slots} 个；" if empty_slots is not None else ""
        return f"背包检查完成：物品 {format_items(items)}；{empty_text}手持 {held}；装备 {format_equipment(payload.get('equipment'))}。"
    if command == "count_item":
        return f"背包中 {payload.get('name', '目标物品')} 数量为 {payload.get('count', 0)}。"
    if command == "has_item":
        name = payload.get("name", "目标物品")
        count = payload.get("count", 0)
        required = payload.get("required", 1)
        status = "满足" if payload.get("has") else "不满足"
        return f"背包中 {name} 当前 x{count}，需求 x{required}，{status}需求。"
    if command == "equip":
        destination = payload.get("destination", "hand")
        return f"已将 {format_item(payload.get('item'))} 装备到 {destination}。"
    return None


def describe_pick(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"拾取失败：{error_reason(action_result, payload)}。"
    picked_items = payload.get("picked_items") or []
    picked_total = payload.get("picked_total")
    total_text = f"{picked_total} 个物品" if isinstance(picked_total, (int, float)) else "物品"
    items_text = _pick_items_text(picked_items)
    if payload.get("stopped") or payload.get("status") == "stopped":
        pending = payload.get("pending_positions") or []
        pending_text = (
            f"剩余 {len(pending)} 个目标位置" if pending else "所有目标位置已处理"
        )
        return f"拾取中断：已拾取 {total_text}（{items_text}）；{pending_text}。"
    completed = payload.get("completed_positions")
    total = payload.get("total_positions")
    covered_text = (
        f"已覆盖 {completed}/{total} 个目标位置"
        if isinstance(completed, (int, float)) and isinstance(total, (int, float))
        else ""
    )
    return f"拾取完成：共拾取 {total_text}（{items_text}）；{covered_text}。"


def _pick_items_text(picked_items: list[dict[str, Any]]) -> str:
    if not picked_items:
        return "无"
    parts = []
    for item in picked_items:
        name = item.get("name") or "unknown"
        count = item.get("count")
        parts.append(f"{name}×{count}" if isinstance(count, (int, float)) else str(name))
    return "、".join(parts)


def _position_text(payload: dict[str, Any]) -> str:
    position = payload.get("position") or {}
    x, y, z = position.get("x"), position.get("y"), position.get("z")
    if all(value is not None for value in (x, y, z)):
        return f"({x}, {y}, {z})"
    return "当前位置"


def describe_drop(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"丢弃失败：{error_reason(action_result, payload)}。"
    items = payload.get("dropped_items") or []
    total = payload.get("dropped_total")
    items_text = _pick_items_text(items)
    total_text = f"，共 {total} 件物品" if isinstance(total, (int, float)) else ""
    return f"在 {_position_text(payload)} 丢弃了 {items_text}{total_text}。"


def describe_give(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"给予失败：{error_reason(action_result, payload)}。"
    target = payload.get("target") or "目标"
    items = payload.get("given_items") or []
    items_text = _pick_items_text(items)
    if payload.get("stopped") or payload.get("status") == "stopped":
        return f"给予中断：已交给 {target} {items_text}，请对方及时拾取。"
    return (
        f"在 {_position_text(payload)} 将 {items_text} 交给了 {target}，"
        "请对方及时拾取。"
    )
