from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_block,
    format_items,
    format_missing,
    format_position,
    result_ok,
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "inspect":
        return describe_inspect(action_result, payload)
    if command == "break":
        return describe_break(action_result, payload)
    if command == "mine":
        return describe_mine(action_result, payload)
    if command == "place":
        return describe_place(action_result, payload)
    if command == "inspect_region":
        return describe_inspect_region(action_result, payload)
    if command == "break_region":
        return describe_break_region(action_result, payload)
    if command == "place_region":
        return describe_place_region(action_result, payload)
    return None


def describe_mine(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"挖掘失败：{error_reason(action_result, payload)}。"
    block_name = payload.get("block_name") or "方块"
    if payload.get("mining") is True:
        return (
            f"正在挖掘 {block_name}：已挖 {payload.get('mined_count', 0)} 个，"
            f"跳过 {payload.get('skipped_count', 0)} 个，破障 {payload.get('obstacles_broken', 0)} 个。"
        )
    reason = payload.get("reason")
    if reason == "group_mined":
        return (
            f"挖掘完成：{block_name} 组共 {payload.get('group_size', 0)} 个，"
            f"挖得 {payload.get('mined_count', 0)} 个，跳过 {payload.get('skipped_count', 0)} 个，"
            f"破障 {payload.get('obstacles_broken', 0)} 个。"
        )
    if reason == "stopped":
        return f"挖掘已停止：{block_name} 已挖 {payload.get('mined_count', 0)} 个。"
    return f"挖掘结束：{block_name} 挖得 {payload.get('mined_count', 0)} 个。"


def describe_inspect(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"查询方块失败：{error_reason(action_result, payload)}。"
    block = payload.get("block")
    air_text = "，该位置为空气" if payload.get("is_air") else ""
    return f"目标 {format_position(payload.get('target'))} 的方块为 {format_block(block)}{air_text}。"


def describe_break(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("error_code") == "target_is_air":
            return f"破坏方块失败：目标 {format_position(payload.get('target'))} 已经是空气。"
        return f"破坏方块失败：{error_reason(action_result, payload)}。"
    parts = [f"已破坏 {format_block(payload.get('broken_block'))}"]
    if payload.get("collect_drops"):
        collected_count = payload.get("collected_count")
        if collected_count is None:
            parts.append("已收集掉落物")
        else:
            parts.append(f"收集 {collected_count} 个掉落物")
    parts.append(f"当前位置现在为 {format_block(payload.get('block_after'))}")
    parts.append(f"背包 {format_items(payload.get('inventory_items'))}")
    return "；".join(parts) + "。"


def describe_place(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("missing"):
            return f"放置方块失败：{format_missing(payload.get('missing'))}。"
        if payload.get("error_code") == "target_occupied":
            return f"放置方块失败：目标位置已有 {format_block(payload.get('target_block_before'))}。"
        if payload.get("error_code") == "no_reference_block":
            return f"放置方块失败：目标 {format_position(payload.get('target'))} 周围没有可依附的参考方块。"
        return f"放置方块失败：{error_reason(action_result, payload)}。"
    return (
        f"已在 {format_position(payload.get('target'))} 放置 "
        f"{format_block(payload.get('target_block_after'))}；"
        f"参考方块为 {format_block(payload.get('reference_block'))}；"
        f"背包 {format_items(payload.get('inventory_items'))}。"
    )


def describe_inspect_region(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"查询区域方块失败：{error_reason(action_result, payload)}。"
    region = format_region(payload.get("region"))
    counts = format_block_counts(payload.get("block_counts"))
    skipped = len(payload.get("skipped") or [])
    skipped_text = f"，跳过 {skipped} 个不可读取位置" if skipped else ""
    return (
        f"已查询区域 {region}：扫描 {payload.get('scanned_count', 0)} 个位置，"
        f"方块统计为 {counts}{skipped_text}。"
    )


def describe_break_region(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return (
            f"区域破坏未完全成功：计划 {payload.get('planned_count', 0)} 个，"
            f"成功 {payload.get('success_count', 0)} 个，失败 {payload.get('failed_count', 0)} 个，"
            f"跳过 {payload.get('skipped_count', 0)} 个；"
            f"原因 {error_reason(action_result, payload)}。"
        )
    collect_text = "（已开启掉落收集）" if payload.get("collect_drops") else ""
    return (
        f"区域破坏完成：计划 {payload.get('planned_count', 0)} 个，"
        f"成功 {payload.get('success_count', 0)} 个，跳过 {payload.get('skipped_count', 0)} 个；"
        f"背包 {format_items(payload.get('inventory_items'))}{collect_text}。"
    )


def describe_place_region(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        if payload.get("missing"):
            return f"区域放置失败：{format_missing(payload.get('missing'))}。"
        return (
            f"区域放置未完全成功：计划 {payload.get('planned_count', 0)} 个，"
            f"成功 {payload.get('success_count', 0)} 个，失败 {payload.get('failed_count', 0)} 个，"
            f"跳过 {payload.get('skipped_count', 0)} 个；"
            f"原因 {error_reason(action_result, payload)}。"
        )
    collect_text = "（已开启掉落收集）" if payload.get("collect_drops") else ""
    return (
        f"区域放置完成：使用 {payload.get('item', 'unknown')}，计划 {payload.get('planned_count', 0)} 个，"
        f"成功 {payload.get('success_count', 0)} 个，跳过 {payload.get('skipped_count', 0)} 个；"
        f"背包 {format_items(payload.get('inventory_items'))}{collect_text}。"
    )


def format_region(region: Any) -> str:
    if not isinstance(region, dict):
        return "未知区域"
    return f"{format_position(region.get('min'))} 到 {format_position(region.get('max'))}"


def format_block_counts(counts: Any, *, limit: int = 8) -> str:
    if not isinstance(counts, dict) or not counts:
        return "无"
    items = sorted(counts.items(), key=lambda pair: (-int(pair[1] or 0), str(pair[0])))
    parts = [f"{name} x{count}" for name, count in items[:limit]]
    if len(items) > limit:
        parts.append(f"另有 {len(items) - limit} 类")
    return "、".join(parts)
