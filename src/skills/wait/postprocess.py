from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    if command != "wait":
        return None
    payload = result_payload(action_result)
    seconds = payload.get("seconds", "指定")
    if not result_ok(action_result, payload):
        return f"等待 {seconds} 秒失败：{error_reason(action_result, payload)}。"
    status = "完成" if payload.get("completed", True) else "已开始"
    return f"等待 {seconds} 秒{status}。"
