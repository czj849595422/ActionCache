"""Prompt builder for the AgenticCache baseline."""

from __future__ import annotations

from typing import Any, Sequence

from src.agents.action import (
    SimpleInstructionRegistry,
    SkillRegistry,
    render_action_space,
)
from src.llm import LLMCallSource, LLMRequest
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory, render_history
from src.tasks.spec import task_description_text
from src.tasks.trade.prompt import (
    TradeRecipeHistory,
    append_trade_recipe_section,
    filter_trade_recipe_history,
)

from .protocol import format_action_instructions
from .schemas import AgenticCacheConfig


def _render_error_history(
    error_history: Sequence[str] | None,
    last_error: str | None,
) -> str:
    errors = [str(error).strip() for error in (error_history or ()) if str(error).strip()]
    if not errors and last_error:
        errors = [str(last_error).strip()]
    if not errors:
        return "无"
    return "\n".join(f"第 {index} 次失败：{error}" for index, error in enumerate(errors, 1))


class AgenticCachePromptBuilder:
    def __init__(self, *, language: str = "zh") -> None:
        if language not in {"zh", "en"}:
            raise ValueError("language 必须是 'zh' 或 'en'")
        self.language = language

    def build_action(
        self,
        *,
        objective: str,
        catalog: str,
        agent_id: str,
        team: str,
        observation_text: str,
        history_text: str,
        error_text: str,
        cache_context: str,
        error_history: Sequence[str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        system = (
            "你是 Minecraft 多智能体任务执行中的单个 Agent 决策器。\n"
            "你只能选择技能目录中允许的一个高层动作，并填入当前观测支持的参数。\n"
            "本 baseline 只在上一个动作结束后进行下一步决策，因此不要请求中断当前动作。\n\n"
            "【任务目标】\n"
            f"{objective or '无'}\n\n"
            "【可用动作】\n"
            f"{catalog or '无'}\n\n"
            "【输出协议】\n"
            f"{format_action_instructions()}"
        )
        user = (
            f"【当前 Agent】\n{agent_id}\n\n"
            f"【队友】\n{team or '无'}\n\n"
            f"【当前观测】\n{observation_text or '尚无有效观测。'}\n\n"
            f"【历史信息】\n{history_text or '暂无历史信息。'}\n\n"
            f"【缓存状态】\n{cache_context or '未命中缓存，需要常规规划。'}\n\n"
            f"【本轮规划失败反馈】\n{_render_error_history(error_history, error_text)}"
        )
        return (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )


def build_planning_request(
    *,
    prompt_builder: AgenticCachePromptBuilder,
    memory: TeamMemory,
    spec: Any,
    registry: SkillRegistry,
    instructions: SimpleInstructionRegistry | None,
    config: AgenticCacheConfig,
    agent_id: str,
    teammates: Sequence[str],
    last_error: str | None,
    cache_context: str,
    trade_recipe_history: TradeRecipeHistory | None = None,
    error_history: Sequence[str] | None = None,
) -> LLMRequest:
    context = memory.to_prompt_context(
        agent_id,
        chat_window=config.chat_window or DEFAULT_TEAM_CHAT_WINDOW,
    )
    observation = context.get("observation")
    observation_text = (
        str(observation).strip() if observation is not None else "尚无有效观测。"
    )
    objective = task_description_text(spec, agent_id=agent_id)
    catalog = render_action_space(
        _chat_filtered_registry(registry)
        if not config.enable_chat_actions
        else registry,
        instructions,
        config.language,
    )
    messages = prompt_builder.build_action(
        objective=objective,
        catalog=catalog,
        agent_id=agent_id,
        team="、".join(teammates) if teammates else "无",
        observation_text=observation_text,
        history_text=render_history(
            filter_trade_recipe_history(context.get("history") or []),
            instructions=(
                instructions if getattr(spec, "category", "") == "trade" else None
            ),
        ),
        error_text=last_error or "无",
        cache_context=cache_context,
        error_history=error_history,
    )
    messages = append_trade_recipe_section(
        messages,
        spec=spec,
        agent_id=agent_id,
        history=trade_recipe_history,
        history_entries=context.get("history") or (),
    )
    return LLMRequest(
        messages=messages,
        metadata={
            "method": "agentic_cache",
            "task_id": getattr(spec, "task_id", ""),
            "agent_id": agent_id,
            "operation": "llm_action",
        },
        source=LLMCallSource(
            component="agentic_cache_planning",
            operation="llm_action",
            task_id=getattr(spec, "task_id", ""),
            agent_id=agent_id,
        ),
    )


def _chat_filtered_registry(registry: SkillRegistry) -> SkillRegistry:
    actions = {
        f"{skill_name}.{command_name}"
        for skill_name, manifest in registry.manifests.items()
        for command_name in manifest.commands
    }
    if registry.allowed_actions is not None:
        actions &= set(registry.allowed_actions)
    return SkillRegistry(
        allowed_actions={action for action in actions if not action.startswith("chat.")}
    )


__all__ = [
    "AgenticCachePromptBuilder",
    "build_planning_request",
]
