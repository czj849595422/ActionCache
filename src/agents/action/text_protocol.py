"""Legacy four-line text decision protocol (kept for non-PPA consumers).

This module is intentionally decoupled from the PPA baseline.  It parses the
older ``ACTION / INTERRUPT / REASON / MESSAGE`` text format into a generic
:class:`TextDecision`; the PPA baseline now uses the paper-style JSON protocol
in ``src/baseline/Parallelized/text_protocol.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .registry import SkillRegistry
from .schemas import ActionMode
from .simple_instructions import (
    SimpleInstructionError,
    SimpleInstructionRegistry,
    parse_action_call,
)
from .validation import SchemaValidationError, validate_schema


class TextProtocolError(ValueError):
    """The model response is not a valid four-line text decision."""


@dataclass
class TextDecision:
    """A parsed legacy text-protocol decision independent of any baseline."""

    action: str
    params: dict[str, Any]
    mode: str
    interrupt: bool
    reason: str
    passive_message: str | None = None
    input_params: dict[str, Any] = field(default_factory=dict)
    input_positional: list[Any] = field(default_factory=list)
    raw_text: str = ""


class FunctionCallTextCodec:
    """Parse four labelled text lines containing one safe skill call."""

    _LABELS = ("ACTION", "INTERRUPT", "REASON", "MESSAGE")

    def format_instructions(self) -> str:
        return (
            "只输出以下四行，不要使用 JSON、Markdown 代码块或附加解释：\n"
            "ACTION: <skill>.<command>(name=value, ...)\n"
            "INTERRUPT: YES 或 NO\n"
            "REASON: 一行简短理由\n"
            "MESSAGE: 一行团队消息；不需要通信时填写 -\n"
            "ACTION 参数使用 Python literal：字符串加引号，布尔值使用 True/False，"
            "数组使用 [...]，对象使用 {...}。"
        )

    def parse(
        self,
        text: str,
        *,
        agent_id: str,
        registry: SkillRegistry,
    ) -> TextDecision:
        values = _parse_protocol_lines(text)
        try:
            action, params, positional = parse_action_call(values["ACTION"])
        except SimpleInstructionError as exc:
            raise TextProtocolError(str(exc)) from exc
        if positional:
            raise TextProtocolError("ACTION only accepts keyword arguments")
        if "." not in action:
            raise TextProtocolError("ACTION 必须是 <skill>.<command> 形式")
        try:
            _, spec = registry.resolve(action)
        except (KeyError, PermissionError) as exc:
            raise TextProtocolError(str(exc)) from exc
        try:
            validate_schema(params, spec.raw.get("params_schema") or {}, path="params")
        except SchemaValidationError as exc:
            raise TextProtocolError(str(exc)) from exc
        return _build_decision(
            action=action,
            params=params,
            values=values,
            agent_id=agent_id,
            spec=spec,
            input_params=params,
            input_positional=[],
        )


def _parse_protocol_lines(text: str) -> dict[str, str]:
    """Parse the strict four-line ACTION/INTERRUPT/REASON/MESSAGE protocol."""
    labels = FunctionCallTextCodec._LABELS
    if not isinstance(text, str) or not text.strip():
        raise TextProtocolError("model response must not be empty")
    if "```" in text:
        raise TextProtocolError("Markdown code fences are not allowed")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != len(labels):
        raise TextProtocolError("model response must contain exactly four non-empty lines")
    values: dict[str, str] = {}
    for expected, line in zip(labels, lines):
        label, separator, value = line.partition(":")
        if not separator or label.strip().upper() != expected:
            raise TextProtocolError(f"expected line label {expected}")
        normalized = value.strip()
        if not normalized:
            raise TextProtocolError(f"{expected} must not be empty")
        values[expected] = normalized
    return values


def _build_decision(
    *,
    action: str,
    params: dict[str, Any],
    values: dict[str, str],
    agent_id: str,
    spec: Any,
    input_params: dict[str, Any] | None = None,
    input_positional: list[Any] | None = None,
) -> TextDecision:
    interrupt_text = values["INTERRUPT"].upper()
    if interrupt_text not in {"YES", "NO"}:
        raise TextProtocolError("INTERRUPT must be YES or NO")
    if ActionMode.CONTINUOUS in spec.modes:
        mode = ActionMode.CONTINUOUS
    elif ActionMode.ONESHOT in spec.modes:
        mode = ActionMode.ONESHOT
    else:
        raise TextProtocolError(f"action has no supported execution mode: {action}")
    return TextDecision(
        action=action,
        params=params,
        mode=mode,
        interrupt=interrupt_text == "YES",
        reason=values["REASON"],
        passive_message=(
            None if values["MESSAGE"] == "-" else values["MESSAGE"]
        ),
        input_params=dict(input_params or {}),
        input_positional=list(input_positional or []),
        raw_text="\n".join(
            f"{label}: {values[label]}" for label in FunctionCallTextCodec._LABELS
        ),
    )


class SimpleInstructionCodec(FunctionCallTextCodec):
    """Four-line text protocol accepting short instruction names.

    ACTION lines may use simple aliases (``chat(message='hi')``) or canonical
    ``<skill>.<command>(...)`` names; aliases are expanded through
    :class:`SimpleInstructionRegistry`, defaults are filled, and essential
    parameters are enforced before schema validation.
    """

    def __init__(self, instructions: SimpleInstructionRegistry | None = None) -> None:
        self.instructions = instructions or SimpleInstructionRegistry()

    def format_instructions(self) -> str:
        return (
            "只输出以下四行，不要使用 JSON、Markdown 代码块或附加解释：\n"
            "ACTION: <简单动作指令>(name=value, ...)\n"
            "INTERRUPT: YES 或 NO\n"
            "REASON: 一行简短理由\n"
            "MESSAGE: 一行团队消息；不需要通信时填写 -\n"
            "ACTION 使用技能目录中的简单指令名（如 chat(message='hi')），"
            "参数使用 Python literal：字符串加引号，布尔值使用 True/False，"
            "数组使用 [...]，对象使用 {...}。"
        )

    def parse(
        self,
        text: str,
        *,
        agent_id: str,
        registry: SkillRegistry,
    ) -> TextDecision:
        values = _parse_protocol_lines(text)
        try:
            name, params, positional = parse_action_call(values["ACTION"])
        except SimpleInstructionError as exc:
            raise TextProtocolError(str(exc)) from exc
        entry = self.instructions.resolve_name(name)
        if entry.variadic is not None and params:
            raise TextProtocolError("variadic ACTION must be fully positional")
        action, filled, mode = self.instructions.expand(name, params, positional)
        try:
            _, spec = registry.resolve(action)
        except (KeyError, PermissionError) as exc:
            raise TextProtocolError(str(exc)) from exc
        try:
            validate_schema(filled, spec.raw.get("params_schema") or {}, path="params")
        except SchemaValidationError as exc:
            raise TextProtocolError(str(exc)) from exc
        return TextDecision(
            action=action,
            params=filled,
            mode=mode,
            interrupt=values["INTERRUPT"].upper() == "YES",
            reason=values["REASON"],
            passive_message=(
                None if values["MESSAGE"] == "-" else values["MESSAGE"]
            ),
            input_params=dict(params),
            input_positional=list(positional),
            raw_text="\n".join(
                f"{label}: {values[label]}" for label in FunctionCallTextCodec._LABELS
            ),
        )
