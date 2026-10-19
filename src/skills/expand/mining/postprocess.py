from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if not command.startswith("mine_"):
        return None
    mineral = command[len("mine_"):]
    payload = result_payload(action_result)
    if not result_ok(action_result, payload):
        code = payload.get("error_code") or error_reason(action_result, payload)
        if code == "mining_config_missing":
            return "挖矿配置缺失：未配置 MINING_ORE_BLOCKS。"
        if code == "ore_not_in_task":
            return f"任务中未包含矿物 {mineral}。"
        if code == "tool_required":
            return f"挖掘 {mineral} 需要更高级工具（{payload.get('required') or '未知'}）。"
        return f"挖掘 {mineral} 失败：{error_reason(action_result, payload)}。"
    if payload.get("mining") is True:
        return (
            f"正在挖掘 {mineral}：已挖 {payload.get('mined_count', 0)} 个，"
            f"跳过坐标 {len(payload.get('skipped_coords') or [])} 个，"
            f"工具不足坐标 {len(payload.get('tool_required_coords') or [])} 个。"
        )
    reason = payload.get("reason")
    if reason == "all_groups_mined":
        return (
            f"挖掘完成：{mineral} 共挖得 {payload.get('mined_count', 0)} 个，"
            f"跳过 {payload.get('skipped_count', 0)} 个，破障 {payload.get('obstacles_broken', 0)} 个。"
        )
    if reason == "stopped":
        return f"挖掘已停止：{mineral} 已挖 {payload.get('mined_count', 0)} 个。"
    return f"挖掘结束：{mineral} 挖得 {payload.get('mined_count', 0)} 个。"
