"""Paper-style JSON decision protocol used between the PPA planner and an LLM.

The model responds with a single JSON object matching the paper's action
records (Appendix A):

.. code-block:: json

    {
      "skill": "chat(message='hi')",
      "interrupt": false,
      "reason": "一行简短理由"
    }

The legacy four-line ``ACTION/INTERRUPT/REASON/MESSAGE`` codecs were moved to
``src/agents/action/text_protocol.py`` and are no longer part of the PPA flow.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from src.agents.action import (
    ActionMode,
    SimpleInstructionError,
    SimpleInstructionRegistry,
    SkillRegistry,
    parse_action_call,
)
from src.agents.action.validation import SchemaValidationError, validate_schema
from src.interface import ActionUnit

from .schemas import PPAProposal


class PPAProtocolError(ValueError):
    """The model response is not a valid PPA decision."""


@runtime_checkable
class DecisionCodec(Protocol):
    """Replaceable model-output protocol for the PPA planner."""

    def format_instructions(self) -> str:
        ...

    def parse(
        self,
        text: str,
        *,
        agent_id: str,
        registry: SkillRegistry,
    ) -> PPAProposal:
        ...


class JsonDecisionCodec:
    """Parse the paper-style JSON decision ``{skill, interrupt, reason}``.

    The ``skill`` value is a function-call string.  When a
    :class:`SimpleInstructionRegistry` is attached, aliases such as
    ``chat(message='hi')`` are expanded (variadic positional forms included);
    otherwise only canonical ``<skill>.<command>(name=value, ...)`` calls are
    accepted.  Markdown `` ```json `` fences are tolerated, matching the
    paper's appendix examples.  Extra JSON fields are ignored, allowing future
    incremental extensions (e.g. a passive-communication message).
    """

    def __init__(
        self,
        instructions: SimpleInstructionRegistry | None = None,
    ) -> None:
        self.instructions = instructions

    def format_instructions(self) -> str:
        if self.instructions is not None:
            skill_hint = "<动作指令>(name=value, ...)（如 chat(message='hi')）"
        else:
            skill_hint = "<skill>.<command>(name=value, ...)"
        return (
           "请进行一步步推理，根据所给信息分析下一步动作的最优选择。"
            "当以下情形发生时可以中断：\n1.发生更紧急的情况；\n"
            "2.当前动作存在错误需要修改；\n"
            "当发生以下情形时不需要中断：\n1.新动作紧急程度不如当前动作；\n"
            "2.新动作与当前动作一致，不需要修改。\n"
            "只输出一个 JSON 对象：\n"
            "{\n"
            '  "reason": "推理内容"\n',
            f'  "skill": "{skill_hint}",\n'
            '  "interrupt": true/false,\n'
            "}\n"
            "skill 参数使用 Python literal：字符串加引号，布尔值使用 True/False，"
            "数组使用 [...]，对象使用 {...}。"
        )

    def parse(
        self,
        text: str,
        *,
        agent_id: str,
        registry: SkillRegistry,
    ) -> PPAProposal:
        payload = _parse_json_decision(text)
        skill = payload["skill"]
        try:
            name, params, positional = parse_action_call(skill)
        except SimpleInstructionError as exc:
            raise PPAProtocolError(str(exc)) from exc
        if self.instructions is not None:
            entry = self.instructions.resolve_name(name)
            if entry.variadic is not None and params:
                raise PPAProtocolError("variadic skill must be fully positional")
            action, filled, mode = self.instructions.expand(name, params, positional)
        else:
            if positional:
                raise PPAProtocolError("skill only accepts keyword arguments")
            if "." not in name:
                raise PPAProtocolError("skill 必须是 <skill>.<command> 形式")
            action, filled, mode = name, params, None
        try:
            _, spec = registry.resolve(action)
        except (KeyError, PermissionError) as exc:
            raise PPAProtocolError(str(exc)) from exc
        if mode is None:
            if ActionMode.CONTINUOUS in spec.modes:
                mode = ActionMode.CONTINUOUS
            elif ActionMode.ONESHOT in spec.modes:
                mode = ActionMode.ONESHOT
            else:
                raise PPAProtocolError(
                    f"action has no supported execution mode: {action}"
                )
        try:
            validate_schema(filled, spec.raw.get("params_schema") or {}, path="params")
        except SchemaValidationError as exc:
            raise PPAProtocolError(str(exc)) from exc
        return PPAProposal(
            action_unit=ActionUnit(
                agent_id=agent_id,
                action=action,
                params=filled,
                mode=mode,
            ),
            interrupt=payload["interrupt"],
            reason=payload["reason"],
            passive_message=None,
            raw_text=text.strip(),
            input_params=dict(params),
            input_positional=list(positional),
        )


def _parse_json_decision(text: str) -> dict[str, Any]:
    """Extract and validate the ``{skill, interrupt, reason}`` JSON payload."""
    if not isinstance(text, str) or not text.strip():
        raise PPAProtocolError("model response must not be empty")
    raw = text.strip()
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) != 3 or parts[0].strip() or parts[2].strip():
            raise PPAProtocolError("Markdown 代码块外不允许附加内容")
        body = parts[1]
        if body.lstrip().startswith("json"):
            body = body.lstrip()[len("json"):]
        raw = body.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PPAProtocolError(f"model response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PPAProtocolError("decision must be a JSON object")
    skill = payload.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        raise PPAProtocolError("skill must be a non-empty string")
    interrupt = payload.get("interrupt")
    if not isinstance(interrupt, bool):
        raise PPAProtocolError("interrupt must be a boolean (true/false)")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PPAProtocolError("reason must be a non-empty string")
    return {
        "skill": skill.strip(),
        "interrupt": interrupt,
        "reason": reason.strip(),
    }


def render_action_call(action: str, params: dict[str, Any] | None = None) -> str:
    """Render a validated action as the model-facing function-call syntax."""
    arguments = ", ".join(
        f"{name}={value!r}" for name, value in (params or {}).items()
    )
    return f"{action}({arguments})"
