"""COAP 规划请求组装：memory 上下文 → LLMRequest。"""

from __future__ import annotations

from typing import Any, Sequence

from src.llm import LLMCallSource, LLMRequest
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory, render_history
from src.tasks.spec import task_description_text
from src.tasks.trade.prompt import (
    TradeRecipeHistory,
    append_trade_recipe_section,
    filter_trade_recipe_history,
)

from .adapters import render_catalog
from .config import COAPConfig
from .prompts import COAPPromptBuilder


def build_planning_request(
    *,
    prompt_builder: COAPPromptBuilder,
    memory: TeamMemory,
    spec: Any,
    registry: Any,
    instructions: Any | None,
    config: COAPConfig,
    agent_id: str,
    teammates: Sequence[str],
    operation: str,
    cache_candidates: Sequence[Any],
    last_error: str | None,
    feedbacks: Sequence[str] = (),
    trade_recipe_history: TradeRecipeHistory | None = None,
) -> LLMRequest:
    """构建一次 COAP 规划 LLM 请求（普通或缓存路径共用）。"""
    chat_window = config.chat_window or DEFAULT_TEAM_CHAT_WINDOW
    context = memory.to_prompt_context(agent_id, chat_window=chat_window)
    observation = context.get("observation")
    observation_text = (
        str(observation).strip() if observation is not None else "尚无有效观测。"
    )
    history_text = render_history(
        filter_trade_recipe_history(context.get("history") or []),
        instructions=instructions,
    )
    catalog = render_catalog(
        registry,
        instructions,
        config.language,
        enable_chat_actions=config.enable_chat_actions,
    )
    objective = task_description_text(spec, agent_id=agent_id)
    messages = prompt_builder.build_action(
        operation=operation,
        objective=objective,
        catalog=catalog,
        agent_id=agent_id,
        team="、".join(teammates) if teammates else "无",
        observation_text=observation_text,
        history_text=history_text,
        error_text=last_error or "无",
        feedback_text=_feedback_text(feedbacks),
        cache_candidates=cache_candidates if operation == "cache_action" else None,
        instructions=instructions,
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
            "method": "coap",
            "task_id": getattr(spec, "task_id", ""),
            "agent_id": agent_id,
            "operation": operation,
        },
        source=LLMCallSource(
            component="coap_planning",
            operation=operation,
            task_id=getattr(spec, "task_id", ""),
            agent_id=agent_id,
        ),
    )


def _feedback_text(feedbacks: Sequence[str]) -> str:
    items = [str(item).strip() for item in feedbacks if str(item).strip()]
    if not items:
        return ""
    return "\n\n【规划反馈】\n" + "\n".join(f"- {item}" for item in items)


__all__ = ["build_planning_request"]
