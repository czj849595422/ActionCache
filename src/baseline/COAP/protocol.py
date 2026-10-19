"""COAP 动作规划输出协议（标签文本格式）。

定时并行规划: <reason>推理</reason><interrupt>true/false</interrupt><action>简写动作与参数</action><chat>可选</chat>
普通规划:    <reason>推理</reason><action>简写动作与参数</action><chat>可选</chat>
缓存动作规划: <action>简写动作与参数</action><chat>可选</chat>

动作段为简写函数调用文本（如 ``fight(target={'name':'zombie','entity_id':100})``），
由 codec 层用 ``parse_action_call`` 解析并展开为规范动作。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
import re
from typing import Any, Literal, Sequence


ActionOperation = Literal[
    "timed_standard_action",
    "standard_action",
    "cache_action",
    "direct_action",
]
DEFAULT_ACTION_READY_KEYWORD = "</action>"


class COAPProtocolError(ValueError):
    """模型响应不符合 COAP 动作规划输出协议。"""


@dataclass
class ParsedActionResponse:
    """一次完整的标签文本动作规划响应解析结果。"""

    reasoning: str
    action_text: str
    interrupt: bool
    communication: str
    cache_candidate_matched: bool
    raw_text: str


@dataclass
class ActionHeadResult:
    """动作段（``</action>`` 之前）解析结果，供流式提前返回使用。"""

    reasoning: str
    action_text: str
    interrupt: bool
    cache_candidate_matched: bool


_REASON_RE = re.compile(r"<reason>(.*?)</reason>", re.DOTALL)
_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL)
_INTERRUPT_RE = re.compile(r"<interrupt>(.*?)</interrupt>", re.DOTALL)
_CHAT_RE = re.compile(r"<chat>(.*?)</chat>", re.DOTALL)


def format_action_instructions(
    operation: ActionOperation,
    *,
    cache_candidate_instruction: str = "",
) -> str:
    """生成动作规划的输出协议说明（system 的【输出】段）。"""
    if operation == "cache_action":
        return (
            "使用以下标签文本协议输出："
            "<action>动作与参数</action>，"
            "如需通信在 <action> 之后追加 <chat>消息</chat>。"
            f"{cache_candidate_instruction}"
        )
    if operation == "timed_standard_action":
        return (
            "高效、简洁进行快速推理，根据所给信息得出下一步动作的最优选择。"
            "当以下情形发生时可以中断：1.发生更紧急的情况；"
            "2.当前动作存在错误需要修改；"
            "当发生以下情形时不需要中断：1.新动作紧急程度不如当前动作；"
            "2.新动作与当前动作一致，不需要修改。"
            "使用以下标签文本协议输出："
            "<reason>推理内容</reason>"
            "<interrupt>true 或 false</interrupt>，"
            "<action>动作与参数</action>，"
            "如需通信在 <action> 之后追加 <chat>消息</chat>。"
        )
    if operation == "direct_action":
        return (
            "请直接根据所给信息选择下一步动作。"
            "使用以下标签文本协议输出："
            "<action>动作与参数</action>，"
            "如需通信在 <action> 之后追加 <chat>消息</chat>。"
        )
    return (
        "高效、简洁进行快速推理，根据所给信息得出下一步动作的最优选择。"
        "使用以下标签文本协议输出："
        "<reason>推理内容</reason>"
        "<action>动作与参数</action>"
        "如需通信在 <action> 之后追加 <chat>消息</chat>。"
    )


def parse_action_response(
    text: str,
    *,
    operation: ActionOperation,
    allow_markdown_fences: bool = True,
    maximum_reasoning_characters: int = 2000,
    maximum_communication_characters: int = 2000,
) -> ParsedActionResponse:
    """解析一次完整的标签文本动作规划响应。"""
    if not isinstance(text, str) or not text.strip():
        raise COAPProtocolError("model response must not be empty")
    source = (
        _strip_outer_code_fence(text) if allow_markdown_fences else text.strip()
    )
    head = parse_action_head(
        source,
        operation=operation,
        maximum_reasoning_characters=maximum_reasoning_characters,
    )
    chat_match = _CHAT_RE.search(source)
    communication = ""
    if chat_match is not None:
        communication = _bounded_text(
            chat_match.group(1),
            "chat",
            maximum_communication_characters,
        )
        _reject_json_or_multiline(communication, "chat")
    return ParsedActionResponse(
        reasoning=head.reasoning,
        action_text=head.action_text,
        interrupt=head.interrupt,
        communication=communication,
        cache_candidate_matched=head.cache_candidate_matched,
        raw_text=text.strip(),
    )


def parse_action_head(
    text: str,
    *,
    operation: ActionOperation,
    maximum_reasoning_characters: int = 2000,
) -> ActionHeadResult:
    """解析动作段（流式 ``</action>`` 触发前的 segment 或完整响应）。"""
    if not isinstance(text, str) or not text.strip():
        raise COAPProtocolError("model response must not be empty")
    source = text.strip()
    action_matches = _ACTION_RE.findall(source)
    if len(action_matches) == 1:
        action_text = action_matches[0].strip()
    elif not action_matches and "<action>" in source:
        # 流式 segment（``</action>`` 触发前）：动作标签未闭合，取其后内容
        action_text = source.split("<action>", 1)[1].strip()
    else:
        raise COAPProtocolError(
            "action response must contain exactly one <action>...</action>"
        )
    if not action_text:
        raise COAPProtocolError("action must not be empty")

    reasoning = ""
    if operation in {"timed_standard_action", "standard_action"}:
        reason_matches = _REASON_RE.findall(source)
        if len(reason_matches) != 1:
            raise COAPProtocolError(
                "standard response must contain exactly one <reason>...</reason>"
            )
        reasoning = _bounded_text(
            reason_matches[0],
            "reason",
            maximum_reasoning_characters,
        )
        _reject_json_text(reasoning, "reason")

    interrupt = False
    if operation == "timed_standard_action":
        interrupt_matches = _INTERRUPT_RE.findall(source)
        if len(interrupt_matches) > 1:
            raise COAPProtocolError(
                "timed standard response must contain at most one "
                "<interrupt>...</interrupt>"
            )
        if interrupt_matches:
            raw = interrupt_matches[0].strip().lower()
            if raw not in {"true", "false"}:
                raise COAPProtocolError("interrupt must be true or false")
            interrupt = raw == "true"

    if operation == "cache_action":
        reason_matches = _REASON_RE.findall(source)
        interrupt_matches = _INTERRUPT_RE.findall(source)
        if reason_matches or interrupt_matches:
            raise COAPProtocolError(
                "cache action response must not contain reason/interrupt tags"
            )

    return ActionHeadResult(
        reasoning=reasoning,
        action_text=action_text,
        interrupt=interrupt,
        cache_candidate_matched=False,
    )


def _bounded_text(value: Any, field_path: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise COAPProtocolError(f"{field_path} must be a string")
    text = value.strip()
    if not text:
        raise COAPProtocolError(f"{field_path} must not be empty")
    if len(text) > maximum:
        raise COAPProtocolError(f"{field_path} exceeds the configured character limit")
    return text


def _reject_json_or_multiline(text: str, field_path: str) -> None:
    if "\n" in text or "\r" in text:
        raise COAPProtocolError(f"{field_path} must be one plain-text paragraph")
    if text.startswith("```"):
        raise COAPProtocolError(f"{field_path} must not use markdown fences")
    try:
        json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        raise COAPProtocolError(f"{field_path} must be plain text, not JSON")


def _reject_json_text(text: str, field_path: str) -> None:
    """拒绝纯 JSON 内容（reason 允许多行）。"""
    if text.startswith("```"):
        raise COAPProtocolError(f"{field_path} must not use markdown fences")
    try:
        json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        raise COAPProtocolError(f"{field_path} must be plain text, not JSON")


def _strip_outer_code_fence(text: str) -> str:
    source = text.strip()
    lines = source.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return source


__all__ = [
    "ActionHeadResult",
    "COAPProtocolError",
    "ParsedActionResponse",
    "format_action_instructions",
    "parse_action_head",
    "parse_action_response",
]
