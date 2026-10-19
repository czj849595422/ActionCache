"""PPA↔COAP 桥接：ActionUnit 互转、执行校验、动作空间渲染、反馈文本。"""

from __future__ import annotations

from collections import OrderedDict
import json
import threading
from typing import Any

from src.agents.action import (
    ActionMode,
    SimpleInstructionError,
    SimpleInstructionRegistry,
    SkillRegistry,
    render_action_space,
)
from src.agents.action.validation import SchemaValidationError, validate_schema
from src.interface import ActionUnit
from src.memory import action_text

from .schemas import Action, COAPProposal


_CATALOG_CACHE_LIMIT = 32
_CATALOG_CACHE_LOCK = threading.RLock()
_CATALOG_CACHE: OrderedDict[tuple[Any, ...], str] = OrderedDict()


def action_unit_to_action(unit: ActionUnit) -> Action:
    """把 PPA 的 ActionUnit 转换为环境无关的 COAP Action。"""
    return Action(
        name=str(unit.action),
        parameters=dict(unit.params or {}),
        agent_id=str(unit.agent_id) if unit.agent_id else None,
    )


def build_action_unit(
    *,
    agent_id: str,
    action: str,
    params: dict[str, Any],
    mode: Any,
) -> ActionUnit:
    return ActionUnit(
        agent_id=agent_id,
        action=action,
        params=dict(params or {}),
        mode=mode,
    )


def resolve_mode(spec: Any) -> str:
    """按技能规格选择执行模式（continuous 优先，与 PPA 一致）。"""
    if ActionMode.CONTINUOUS in spec.modes:
        return ActionMode.CONTINUOUS
    if ActionMode.ONESHOT in spec.modes:
        return ActionMode.ONESHOT
    raise ValueError(f"action has no supported execution mode")


def exec_check(
    action: str,
    params: dict[str, Any],
    registry: SkillRegistry,
) -> tuple[bool, str | None]:
    """动作可执行性校验：技能存在 + 允许列表 + 参数 schema。"""
    try:
        _, spec = registry.resolve(action)
    except (KeyError, PermissionError) as exc:
        return False, str(exc)
    try:
        validate_schema(
            params,
            spec.raw.get("params_schema") or {},
            path="params",
        )
    except SchemaValidationError as exc:
        return False, str(exc)
    return True, None


def expand_action_name(
    name: str,
    parameters: dict[str, Any],
    *,
    instructions: SimpleInstructionRegistry | None,
    registry: SkillRegistry,
    enable_chat_actions: bool = False,
    positional: list[Any] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """把协议动作名展开为规范动作（支持简写 alias 与完整名）。"""
    if instructions is None:
        if "." not in name:
            raise ValueError(f"动作必须是 <skill>.<command> 形式: {name!r}")
        canonical, filled, mode = name, dict(parameters), None
    else:
        try:
            canonical, filled, mode = instructions.expand(
                name,
                dict(parameters),
                list(positional or []),
            )
        except (KeyError, SimpleInstructionError) as exc:
            raise ValueError(str(exc)) from exc
    if not enable_chat_actions and canonical.startswith("chat."):
        raise ValueError(
            f"COAP 屏蔽 chat 动作（主动通信走 communication 段）: {canonical}"
        )
    ok, error = exec_check(canonical, filled, registry)
    if not ok:
        raise ValueError(error or f"动作不可执行: {canonical}")
    _, spec = registry.resolve(canonical)
    return canonical, filled, resolve_mode(spec)


def build_chat_filtered_registry(registry: SkillRegistry) -> SkillRegistry:
    """构造排除 chat.* 的 SkillRegistry 视图，供动作空间渲染与权限校验。"""
    actions = {
        f"{skill_name}.{command_name}"
        for skill_name, manifest in registry.manifests.items()
        for command_name in manifest.commands
    }
    base = registry.allowed_actions
    if base is not None:
        actions &= set(base)
    actions = {action for action in actions if not action.startswith("chat.")}
    return SkillRegistry(allowed_actions=actions)


def render_catalog(
    registry: SkillRegistry,
    instructions: SimpleInstructionRegistry | None,
    language: str,
    *,
    enable_chat_actions: bool = False,
) -> str:
    """渲染动作空间；COAP 默认过滤 chat 动作。"""
    cache_key = (
        _registry_catalog_snapshot(registry),
        _instruction_catalog_snapshot(instructions),
        str(language),
        bool(enable_chat_actions),
    )
    with _CATALOG_CACHE_LOCK:
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None:
            _CATALOG_CACHE.move_to_end(cache_key)
            return cached

        # Keep the first render single-flight. This prevents concurrent workers
        # from rescanning the same skill manifests during episode startup.
        if enable_chat_actions:
            catalog = render_action_space(registry, instructions, language)
        else:
            filtered = build_chat_filtered_registry(registry)
            catalog = render_action_space(filtered, instructions, language)
        _CATALOG_CACHE[cache_key] = catalog
        _CATALOG_CACHE.move_to_end(cache_key)
        while len(_CATALOG_CACHE) > _CATALOG_CACHE_LIMIT:
            _CATALOG_CACHE.popitem(last=False)
        return catalog


def _registry_catalog_snapshot(registry: SkillRegistry) -> tuple[Any, ...]:
    """Return the manifest content that can affect rendered action text."""
    allowed = registry.allowed_actions
    allowed_snapshot = None if allowed is None else tuple(sorted(allowed))
    manifests = []
    for skill_name, manifest in sorted(registry.manifests.items()):
        manifests.append(
            (
                skill_name,
                manifest.version,
                manifest.runtime,
                manifest.entry,
                tuple(sorted(manifest.commands)),
                json.dumps(
                    manifest.raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    return allowed_snapshot, tuple(manifests)


def _instruction_catalog_snapshot(
    instructions: SimpleInstructionRegistry | None,
) -> tuple[Any, ...] | None:
    """Return instruction mappings used by the catalog renderer."""
    if instructions is None:
        return None
    by_action = getattr(instructions, "by_action", {})
    return tuple(
        (
            action,
            instruction.alias,
            instruction.params,
            instruction.description,
            instruction.description_en,
            instruction.variadic,
            instruction.default_count,
            instruction.optional,
        )
        for action, instruction in sorted(by_action.items())
    )


def broadcast_text(
    agent_id: str,
    proposal: COAPProposal,
    result_status: str,
    result_error: str | None,
    instructions: SimpleInstructionRegistry | None,
) -> str:
    """生成动作反馈文本（被动通信，不调用 LLM）。"""
    call = f"执行{_proposal_action_text(proposal, instructions)}"
    if result_status == "interrupted":
        suffix = "中断"
    elif result_status == "failed":
        reason = (result_error or "").strip()
        suffix = f"失败：{reason}" if reason else "失败"
    else:
        suffix = "完成"
    return f"{agent_id}：{call} {suffix}"


def _proposal_action_text(
    proposal: COAPProposal,
    instructions: SimpleInstructionRegistry | None,
) -> str:
    params = proposal.input_params or proposal.action_unit.params
    return action_text(
        proposal.action_unit.action,
        params,
        instructions=instructions,
        positional=proposal.input_positional,
    )


__all__ = [
    "action_unit_to_action",
    "broadcast_text",
    "build_action_unit",
    "build_chat_filtered_registry",
    "exec_check",
    "expand_action_name",
    "render_catalog",
    "resolve_mode",
]
