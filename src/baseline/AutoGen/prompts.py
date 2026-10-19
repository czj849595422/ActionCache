"""Prompt construction for the AutoGen-style A2A baseline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from src.baseline.COAP.adapters import render_catalog
from src.llm import LLMCallSource, LLMRequest
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory, render_history
from src.tasks.spec import task_description_text
from src.tasks.trade.prompt import render_trade_team_professions
from src.tasks.trade.prompt import (
    TradeRecipeHistory,
    append_trade_recipe_section,
    filter_trade_recipe_history,
)

from .a2a import A2AStore
from .perception import render_commander_perception
from .schemas import A2AMessage, AutoGenConfig, SubtaskAssignment

COMMANDER_DESCRIPTION_DIR = Path(__file__).resolve().parent / "descriptions"


def build_commander_request(
    *,
    memory: TeamMemory,
    a2a: A2AStore,
    spec: Any,
    config: AutoGenConfig,
    eligible_recipients: Sequence[str],
    last_error: str | None = None,
    error_history: Sequence[str] | None = None,
) -> LLMRequest:
    objective = _commander_objective_text(spec)
    observations = render_commander_perception(memory=memory, spec=spec)
    public_results = render_public_results(
        memory,
        agent_id=config.commander_id,
        limit=config.public_result_window,
    )
    states = _states_text(a2a)
    system = (
        "You are the Commander for a Minecraft multi-agent task. "
        "You do not control Minecraft directly. Assign short-horizon subtasks "
        "only to eligible idle executors. Do not output concrete Minecraft actions."
    )
    user = (
        f"Task objective:\n{objective}\n\n"
        f"Eligible executors: {', '.join(eligible_recipients) or 'none'}\n\n"
        f"Executor states:\n{states}\n\n"
        f"Commander perception summary:\n{observations}\n\n"
        f"【历史信息】\n{public_results}\n\n"
        f"【本轮规划失败反馈】\n{_render_error_history(error_history, last_error)}\n\n"
        "Return exactly:\n"
        "<reason>why these assignments are appropriate</reason>\n"
        "<assignments>[{\"recipient\":\"Alice\",\"subtask_id\":\"s1\","
        "\"objective\":\"...\",\"success_criteria\":\"...\","
        "\"constraints\":[\"...\"],\"max_planning_rounds\":5}]</assignments>\n"
        "Use an empty JSON array when no new assignment should be issued."
    )
    return LLMRequest(
        messages=({"role": "system", "content": system}, {"role": "user", "content": user}),
        metadata={
            "method": "autogen",
            "task_id": getattr(spec, "task_id", ""),
            "agent_id": config.commander_id,
            "operation": "commander_assign",
        },
        source=LLMCallSource(
            component="autogen_commander",
            operation="commander_assign",
            task_id=getattr(spec, "task_id", ""),
            agent_id=config.commander_id,
        ),
    )


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


def build_executor_request(
    *,
    memory: TeamMemory,
    a2a: A2AStore,
    spec: Any,
    registry: Any,
    instructions: Any,
    config: AutoGenConfig,
    agent_id: str,
    teammates: Sequence[str],
    assignment: SubtaskAssignment,
    last_error: str | None = None,
    trade_recipe_history: TradeRecipeHistory | None = None,
) -> LLMRequest:
    observation_entry = memory.get_observation(agent_id)
    observation = (
        str(observation_entry.content).strip()
        if observation_entry is not None
        else "No valid observation yet."
    )
    catalog = render_catalog(
        registry,
        instructions,
        config.language,
        enable_chat_actions=config.enable_chat_actions,
    )
    history_entries = memory.prompt_history(
        agent_id,
        team_limit=config.public_result_window,
        system_limit=config.public_result_window,
    )
    public_results = render_history(
        filter_trade_recipe_history(history_entries),
        instructions=(
            instructions if getattr(spec, "category", "") == "trade" else None
        ),
    )
    system = (
        "You are a Minecraft executor agent. You must handle exactly one "
        "Commander subtask. In one LLM response, decide whether the subtask is "
        "done. If not done, choose the next executable action."
    )
    user = (
        f"Task objective:\n{_executor_objective_text(spec, agent_id)}\n\n"
        f"You are: {agent_id}\n"
        f"Teammates: {', '.join(teammates) if teammates else 'none'}\n\n"
        f"Current Commander assignment:\n{_assignment_text(assignment)}\n\n"
        f"Your latest observation:\n{observation}\n\n"
        f"【历史信息】\n{public_results}\n\n"
        f"Available actions:\n{catalog}\n\n"
        f"Previous planning error: {last_error or 'none'}\n\n"
        "Return exactly one of these forms:\n"
        "<status>continue</status><reason>...</reason><action>action(args)</action>\n"
        "or\n"
        "<status>completed|failed|blocked</status><reason>...</reason>"
        "<report>private report for Commander</report>\n"
        "Do not split completion checking and action planning into separate calls."
    )
    messages = append_trade_recipe_section(
        ({"role": "system", "content": system}, {"role": "user", "content": user}),
        spec=spec,
        agent_id=agent_id,
        history=trade_recipe_history,
        history_entries=history_entries,
    )
    return LLMRequest(
        messages=messages,
        metadata={
            "method": "autogen",
            "task_id": getattr(spec, "task_id", ""),
            "agent_id": agent_id,
            "operation": "executor_subtask",
            "subtask_id": assignment.subtask_id,
        },
        source=LLMCallSource(
            component="autogen_executor",
            operation="executor_subtask",
            task_id=getattr(spec, "task_id", ""),
            agent_id=agent_id,
        ),
    )


def render_public_results(
    memory: TeamMemory,
    *,
    agent_id: str = "",
    limit: int = DEFAULT_TEAM_CHAT_WINDOW,
    instructions: Any | None = None,
) -> str:
    return render_history(
        filter_trade_recipe_history(memory.prompt_history(
            agent_id,
            team_limit=limit,
            system_limit=limit,
        )),
        instructions=instructions,
    )


def render_a2a_messages(
    messages: Sequence[A2AMessage],
    *,
    include_reports: bool = True,
) -> str:
    if not messages:
        return "No A2A messages yet."
    lines = []
    for message in messages:
        content = message.content
        if message.message_type == "assignment":
            text = (
                f"assignment {content.get('subtask_id')}: "
                f"{content.get('objective')} | success: {content.get('success_criteria')}"
            )
        else:
            if include_reports:
                text = (
                    f"report {content.get('subtask_id')} "
                    f"[{content.get('status')}]: {content.get('report')}"
                )
            else:
                text = (
                    f"report {content.get('subtask_id')} "
                    f"[{content.get('status')}]"
                )
        lines.append(
            f"- {message.sender} -> {message.recipient}: {text}"
        )
    return "\n".join(lines)


def _states_text(a2a: A2AStore) -> str:
    states = a2a.states()
    if not states:
        return "No executor states."
    lines = []
    for agent_id, state in states.items():
        assignment = state.active_assignment
        if assignment is None:
            task_text = "none"
        else:
            task_text = f"{assignment.subtask_id}: {assignment.objective}"
        lines.append(
            f"- {agent_id}: {state.status}, rounds={state.planning_round}, task={task_text}"
        )
    return "\n".join(lines)


def _assignment_text(assignment: SubtaskAssignment) -> str:
    constraints = ", ".join(assignment.constraints) if assignment.constraints else "none"
    return (
        f"id={assignment.subtask_id}\n"
        f"objective={assignment.objective}\n"
        f"success_criteria={assignment.success_criteria}\n"
        f"constraints={constraints}\n"
        f"max_planning_rounds={assignment.max_planning_rounds}"
    )


def _commander_objective_text(spec: Any) -> str:
    if getattr(spec, "category", "") == "trade":
        return render_trade_team_professions(spec)
    task_id = str(getattr(spec, "task_id", "") or "").strip()
    if not task_id:
        return ""
    path = COMMANDER_DESCRIPTION_DIR / f"{task_id}.commander.description.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _executor_objective_text(spec: Any, agent_id: str) -> str:
    return task_description_text(spec, agent_id=agent_id)


__all__ = [
    "build_commander_request",
    "build_executor_request",
    "render_a2a_messages",
    "render_public_results",
]
