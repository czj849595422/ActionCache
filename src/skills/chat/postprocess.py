from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import error_reason, result_ok, result_payload


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "say":
        if not result_ok(action_result, payload):
            return f"发送聊天失败：{error_reason(action_result, payload)}。"
        return f"已发送：{payload.get('message')}。"
    if command == "whisper":
        if not result_ok(action_result, payload):
            return f"私聊失败：{error_reason(action_result, payload)}。"
        return f"已私聊 {payload.get('username')}：{payload.get('message')}。"
    return None
