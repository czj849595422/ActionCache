from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if command != "harvest":
        return None
    payload = result_payload(action_result)
    if not result_ok(action_result, payload):
        return describe_failure(action_result, payload)
    crop = payload.get("crop") or "作物"
    harvested = payload.get("harvested", 0)
    reason = payload.get("reason")
    if payload.get("harvesting") is True:
        remaining = payload.get("remaining")
        remaining_text = f"，剩余约 {remaining} 株" if isinstance(remaining, int) else ""
        return f"正在收获 {crop}：已收获 {harvested} 株{remaining_text}。"
    if reason == "count_reached":
        return f"已收获指定数量的 {crop}：共 {harvested} 株。"
    if reason == "all_harvested":
        return f"范围内成熟 {crop} 已全部收获：共 {harvested} 株。"
    if reason == "max_duration_exceeded":
        return f"收获达到时限停止：已收获 {harvested} 株 {crop}。"
    return f"收获完成：{harvested} 株 {crop}。"


def describe_failure(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    code = payload.get("error_code") or error_reason(action_result, payload)
    crop = payload.get("crop") or "作物"
    if code == "invalid_params":
        return f"收获失败：参数无效（必须提供 crop 名称）。"
    if code == "unknown_crop":
        return f"收获失败：不支持的作物 {crop}。"
    if code == "no_mature_crop":
        return f"收获失败：范围 {payload.get('range')} 内没有成熟的 {crop}。"
    if code == "harvest_failed":
        return f"收获失败：{payload.get('reason') or '未知原因'}。"
    return f"收获失败：{code}。"
