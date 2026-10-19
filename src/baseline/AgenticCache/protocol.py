"""AgenticCache LLM action-output protocol.

Unlike COAP, this protocol has no racing path and no interrupt field.  The
runner decides only when the previous action has finished.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


class AgenticCacheProtocolError(ValueError):
    """Raised when a model response does not match the local protocol."""


@dataclass(frozen=True)
class ParsedActionResponse:
    reasoning: str
    action_text: str
    communication: str
    raw_text: str


_REASON_RE = re.compile(r"<reason>(.*?)</reason>", re.DOTALL)
_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL)
_CHAT_RE = re.compile(r"<chat>(.*?)</chat>", re.DOTALL)


def format_action_instructions() -> str:
    return (
        "使用以下标签文本协议输出："
        "<reason>简要说明为什么选择该动作</reason>"
        "<action>动作与参数</action>"
        "；如需通信，在 <action> 之后追加 <chat>一段消息</chat>。"
        "不要输出 interrupt 字段。"
        "必须使用【可用动作】中的简写动作名，动作格式示例：wait(seconds=1)。"
    )


def parse_action_response(
    text: str,
    *,
    allow_markdown_fences: bool = True,
    maximum_reasoning_characters: int = 2000,
    maximum_communication_characters: int = 2000,
) -> ParsedActionResponse:
    if not isinstance(text, str) or not text.strip():
        raise AgenticCacheProtocolError("model response must not be empty")
    source = _strip_outer_code_fence(text) if allow_markdown_fences else text.strip()
    reason_matches = _REASON_RE.findall(source)
    action_matches = _ACTION_RE.findall(source)
    if len(reason_matches) != 1:
        raise AgenticCacheProtocolError(
            "response must contain exactly one <reason>...</reason>"
        )
    if len(action_matches) != 1:
        raise AgenticCacheProtocolError(
            "response must contain exactly one <action>...</action>"
        )
    reasoning = _bounded_text(
        reason_matches[0],
        "reason",
        maximum_reasoning_characters,
    )
    _reject_json_text(reasoning, "reason")
    action_text = action_matches[0].strip()
    if not action_text:
        raise AgenticCacheProtocolError("action must not be empty")
    chat_matches = _CHAT_RE.findall(source)
    if len(chat_matches) > 1:
        raise AgenticCacheProtocolError("response must contain at most one chat tag")
    communication = ""
    if chat_matches:
        communication = _bounded_text(
            chat_matches[0],
            "chat",
            maximum_communication_characters,
        )
        _reject_json_or_multiline(communication, "chat")
    return ParsedActionResponse(
        reasoning=reasoning,
        action_text=action_text,
        communication=communication,
        raw_text=text.strip(),
    )


def _bounded_text(value: Any, field_path: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AgenticCacheProtocolError(f"{field_path} must be a string")
    text = value.strip()
    if not text:
        raise AgenticCacheProtocolError(f"{field_path} must not be empty")
    if len(text) > maximum:
        raise AgenticCacheProtocolError(f"{field_path} exceeds the configured limit")
    return text


def _reject_json_text(text: str, field_path: str) -> None:
    if text.startswith("```"):
        raise AgenticCacheProtocolError(f"{field_path} must not use markdown fences")
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return
    raise AgenticCacheProtocolError(f"{field_path} must be plain text, not JSON")


def _reject_json_or_multiline(text: str, field_path: str) -> None:
    if "\n" in text or "\r" in text:
        raise AgenticCacheProtocolError(f"{field_path} must be one paragraph")
    _reject_json_text(text, field_path)


def _strip_outer_code_fence(text: str) -> str:
    source = text.strip()
    lines = source.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```text"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return source


__all__ = [
    "AgenticCacheProtocolError",
    "ParsedActionResponse",
    "format_action_instructions",
    "parse_action_response",
]
