"""AgenticCache action codec and execution validation."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.agents.action import (
    ActionMode,
    SimpleInstructionError,
    SimpleInstructionRegistry,
    SkillRegistry,
    parse_action_call,
)
from src.agents.action.validation import SchemaValidationError, validate_schema
from src.interface import ActionUnit

from .protocol import ParsedActionResponse, parse_action_response
from .schemas import ActionPlan, AgenticCacheProposal
from .templates import (
    AgenticCacheTemplateError,
    instantiate_template_plan,
    render_action_call,
    template_plan_from_call,
    template_plan_from_unit,
)


class AgenticCacheCodecError(ValueError):
    pass


def action_unit_to_plan(
    unit: ActionUnit,
    *,
    instructions: SimpleInstructionRegistry | None = None,
    binding_context: Mapping[str, Any] | None = None,
    input_params: Mapping[str, Any] | None = None,
    input_positional: Sequence[Any] | None = None,
) -> ActionPlan:
    if instructions is None:
        return ActionPlan(
            name=str(unit.action),
            parameters=dict(unit.params or {}),
            canonical_name=str(unit.action),
            canonical_parameters=dict(unit.params or {}),
        )
    return template_plan_from_unit(
        unit,
        instructions=instructions,
        binding_context=binding_context,
        input_params=input_params,
        input_positional=input_positional,
    )


def resolve_action_mode(spec: Any) -> str:
    if ActionMode.CONTINUOUS in spec.modes:
        return ActionMode.CONTINUOUS
    if ActionMode.ONESHOT in spec.modes:
        return ActionMode.ONESHOT
    raise AgenticCacheCodecError("action has no supported execution mode")


def exec_check(
    action: str,
    params: dict[str, Any],
    registry: SkillRegistry,
) -> tuple[bool, str | None]:
    try:
        _, spec = registry.resolve(action)
    except (KeyError, PermissionError) as exc:
        return False, str(exc)
    try:
        validate_schema(params, spec.raw.get("params_schema") or {}, path="params")
    except SchemaValidationError as exc:
        return False, str(exc)
    return True, None


def build_action_unit(
    *,
    agent_id: str,
    action: str,
    params: dict[str, Any],
    registry: SkillRegistry,
) -> ActionUnit:
    ok, error = exec_check(action, params, registry)
    if not ok:
        raise AgenticCacheCodecError(error or f"动作不可执行: {action}")
    _, spec = registry.resolve(action)
    return ActionUnit(
        agent_id=agent_id,
        action=action,
        params=dict(params or {}),
        mode=resolve_action_mode(spec),
    )


class AgenticCacheDecisionCodec:
    """Convert model text or cached plans into executable proposals."""

    def __init__(
        self,
        registry: SkillRegistry,
        instructions: SimpleInstructionRegistry | None = None,
        *,
        enable_chat_actions: bool = False,
    ) -> None:
        self.registry = registry
        self.instructions = instructions
        self.enable_chat_actions = enable_chat_actions

    def parse_complete(
        self,
        text: str,
        *,
        agent_id: str,
        binding_context: Mapping[str, Any] | None = None,
    ) -> AgenticCacheProposal:
        parsed = parse_action_response(text)
        return self._to_proposal(
            parsed,
            agent_id=agent_id,
            path="llm",
            binding_context=binding_context,
        )

    def from_cached_plan(
        self,
        plan: ActionPlan,
        *,
        agent_id: str,
        previous_action: ActionPlan,
        state_metadata: dict[str, float],
        cache_entry_id: str,
        reason: str = "",
        binding_context: Mapping[str, Any] | None = None,
    ) -> AgenticCacheProposal:
        try:
            (
                canonical,
                filled,
                mode,
                input_params,
                input_positional,
                executed_plan,
            ) = instantiate_template_plan(
                plan,
                binding_context=binding_context,
                instructions=self.instructions,
            )
        except AgenticCacheTemplateError as exc:
            raise AgenticCacheCodecError(str(exc)) from exc
        ok, error = exec_check(canonical, filled, self.registry)
        if not ok:
            raise AgenticCacheCodecError(error or f"动作不可执行: {canonical}")
        unit = ActionUnit(
            agent_id=agent_id,
            action=canonical,
            params=dict(filled),
            mode=mode,
        )
        return AgenticCacheProposal(
            action_unit=unit,
            reason=reason or "cache transition",
            path="cache",
            previous_action=previous_action,
            state_metadata=dict(state_metadata),
            cache_entry_id=cache_entry_id,
            input_params=dict(input_params),
            input_positional=list(input_positional),
            cache_action=executed_plan,
        )

    def _to_proposal(
        self,
        parsed: ParsedActionResponse,
        *,
        agent_id: str,
        path: str,
        binding_context: Mapping[str, Any] | None = None,
    ) -> AgenticCacheProposal:
        try:
            name, params, positional = parse_action_call(parsed.action_text)
        except SimpleInstructionError as exc:
            raise AgenticCacheCodecError(str(exc)) from exc
        if "." in name:
            raise AgenticCacheCodecError(
                f"AgenticCache requires shorthand action aliases, got {name!r}"
            )
        try:
            canonical, filled = self._expand_action(
                name,
                dict(params),
                list(positional),
            )
        except ValueError as exc:
            raise AgenticCacheCodecError(str(exc)) from exc
        unit = build_action_unit(
            agent_id=agent_id,
            action=canonical,
            params=filled,
            registry=self.registry,
        )
        cache_action = template_plan_from_call(
            alias=name,
            input_params=params,
            input_positional=positional,
            canonical_name=canonical,
            canonical_parameters=filled,
            binding_context=binding_context,
            action_text=parsed.action_text
            or render_action_call(name, params, positional),
        )
        return AgenticCacheProposal(
            action_unit=unit,
            reason=parsed.reasoning,
            communication=parsed.communication,
            path=path,
            raw_text=parsed.raw_text,
            input_params=dict(params),
            input_positional=list(positional),
            cache_action=cache_action,
        )

    def _expand_action(
        self,
        name: str,
        params: dict[str, Any],
        positional: list[Any],
    ) -> tuple[str, dict[str, Any]]:
        if self.instructions is not None:
            try:
                canonical, filled, _mode = self.instructions.expand(
                    name,
                    params,
                    positional,
                )
            except (KeyError, SimpleInstructionError) as exc:
                raise ValueError(str(exc)) from exc
        else:
            if "." not in name:
                raise ValueError(f"动作必须是 <skill>.<command> 形式: {name!r}")
            canonical, filled = name, dict(params)
        if not self.enable_chat_actions and canonical.startswith("chat."):
            raise ValueError("AgenticCache 默认屏蔽 chat 动作，通信请使用 <chat>")
        ok, error = exec_check(canonical, filled, self.registry)
        if not ok:
            raise ValueError(error or f"动作不可执行: {canonical}")
        return canonical, filled


__all__ = [
    "AgenticCacheCodecError",
    "AgenticCacheDecisionCodec",
    "action_unit_to_plan",
    "build_action_unit",
    "exec_check",
    "resolve_action_mode",
]
