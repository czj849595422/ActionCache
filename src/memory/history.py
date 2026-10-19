"""统一团队历史事件的校验与 prompt 渲染。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

HISTORY_KEY = "history"
HISTORY_SOURCES = {"self", "teammate", "system"}
HISTORY_EVENT_TYPES = {"planning", "communication", "execution", "system"}


def validate_history_event(
    *,
    source: str,
    event_type: str,
    content: Mapping[str, Any],
) -> None:
    if source not in HISTORY_SOURCES:
        raise ValueError(f"history source must be one of {sorted(HISTORY_SOURCES)}")
    if event_type not in HISTORY_EVENT_TYPES:
        raise ValueError(
            f"history event_type must be one of {sorted(HISTORY_EVENT_TYPES)}"
        )
    if not isinstance(content, Mapping):
        raise TypeError("history content must be a mapping")


def action_text(
    action: Any,
    params: Any = None,
    *,
    instructions: Any | None = None,
    positional: Sequence[Any] | None = None,
) -> str:
    """Render an action, optionally using the configured simple alias.

    ``params`` should contain the parameters supplied by the model. Callers
    that have already expanded an action can pass ``positional`` to preserve
    the compact positional form in history.
    """
    name = str(action or "无")
    if instructions is not None and name != "无":
        resolver = getattr(instructions, "instruction_for_action", None)
        if callable(resolver):
            instruction = resolver(name)
            if instruction is not None:
                name = str(getattr(instruction, "alias", name) or name)
    if positional:
        arguments = ", ".join(repr(value) for value in positional)
    elif isinstance(params, Mapping) and params:
        arguments = ", ".join(f"{key}={value!r}" for key, value in params.items())
    else:
        return name
    return f"{name}({arguments})"


def render_history(
    entries: Sequence[Mapping[str, Any]],
    *,
    instructions: Any | None = None,
) -> str:
    """渲染已完成来源裁剪和时间排序的历史视图。"""
    if not entries:
        return "暂无历史信息。"
    rendered = [
        _render_entry(entry, instructions=instructions)
        for entry in entries
    ]
    rendered = [item for item in rendered if item.strip()]
    return "\n\n".join(rendered) if rendered else "暂无历史信息。"


def _render_entry(
    entry: Mapping[str, Any],
    *,
    instructions: Any | None = None,
) -> str:
    source = entry.get("source") or "teammate"
    agent_id = entry.get("agent_id") or "unknown"
    content = entry.get("content")
    content = content if isinstance(content, Mapping) else {"message": content}

    if source == "system":
        return f"[系统]\n{content.get('message') or content.get('text') or ''}"
    if source == "teammate":
        header = f"[队友: {agent_id}]"
        parts: list[str] = []
        if content.get("public_broadcast"):
            parts.append(f"公共广播：{content.get('public_broadcast')}")
        if entry.get("event_type") == "communication":
            return f"{header}\n通信：{content.get('message') or ''}"
        if content.get("communication") and content.get("communication") != content.get("public_broadcast"):
            parts.append(f"通信：{content.get('communication')}")
        if not content.get("result_status") and not content.get("result_description"):
            return f"{header}\n" + "\n".join(parts) if parts else ""
        action = action_text(
            content.get("selected_action") or content.get("action"),
            content.get("params") or content.get("input_params"),
            instructions=instructions,
            positional=content.get("input_positional"),
        )
        status = content.get("result_status") or content.get("status") or "unknown"
        parts.append(f"动作完成结果：{action}（状态：{status}）")
        return f"{header}\n" + "\n".join(parts)

    lines: list[str] = ["[自身]"]
    action_params = content.get("params") or content.get("input_params")
    action_positional = content.get("input_positional")
    if content.get("cache_plan") is not None:
        lines.append(
            "缓存规划："
            + action_text(
                content.get("cache_plan"),
                action_params,
                instructions=instructions,
                positional=action_positional,
            )
        )
    if content.get("standard_plan") is not None:
        lines.append(
            "普通规划："
            + action_text(
                content.get("standard_plan"),
                action_params,
                instructions=instructions,
                positional=action_positional,
            )
        )
    if content.get("selected_action") is not None:
        selected = action_text(
            content.get("selected_action"),
            action_params,
            instructions=instructions,
            positional=action_positional,
        )
        selected_source = content.get("selected_source")
        if selected_source:
            selected = f"{selected}（来源：{selected_source}）"
        lines.append(f"动作选择：{selected}")
    if content.get("reasoning"):
        lines.append(f"推理内容：{content.get('reasoning') or '无'}")
    if content.get("communication"):
        lines.append(f"通信：{content.get('communication') or '无'}")
    if content.get("public_broadcast") and content.get("public_broadcast") != content.get("communication"):
        lines.append(f"公共广播：{content.get('public_broadcast')}")
    if (
        content.get("result_description")
        or content.get("result_status")
    ):
        description = content.get("result_description") or ""
        status = content.get("result_status")
        result = description
        if status and description:
            result = f"{description}（状态：{status}）"
        elif status:
            result = f"状态：{status}"
        lines.append(f"动作完成结果：{result}")
    if len(lines) == 1:
        message = content.get("message") or content.get("text") or ""
        if not message:
            return ""
        lines.append(str(message))
    return "\n".join(lines)


__all__ = [
    "HISTORY_EVENT_TYPES",
    "HISTORY_KEY",
    "HISTORY_SOURCES",
    "action_text",
    "render_history",
    "validate_history_event",
]
