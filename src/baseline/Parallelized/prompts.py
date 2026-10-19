"""Text-only prompt assembly for the PPA planning workers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from src.agents.action import SimpleInstructionRegistry, SkillRegistry, render_action_space
from src.agents.action.result_postprocessor import postprocess_action_result
from src.llm import LLMCallSource, LLMRequest
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory, render_history
from src.tasks import TaskSpec
from src.tasks.spec import task_description_text
from src.tasks.trade.prompt import (
    TradeRecipeHistory,
    append_trade_recipe_section,
    filter_trade_recipe_history,
)

from .schemas import PPAConfig
from .text_protocol import DecisionCodec, render_action_call


TaskStateProvider = Callable[[], dict[str, Any]]
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompts"


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


class PPAPromptBuilder:
    """Render method state as two text messages without JSON serialization."""

    def __init__(
        self,
        *,
        spec: TaskSpec,
        memory: TeamMemory,
        codec: DecisionCodec,
        config: PPAConfig,
        instructions: SimpleInstructionRegistry | None = None,
        template_dir: str | Path | None = None,
        task_state_provider: TaskStateProvider | None = None,
        trade_recipe_history: TradeRecipeHistory | None = None,
    ) -> None:
        self.spec = spec
        self.memory = memory
        self.codec = codec
        self.config = config
        self.instructions = instructions
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else DEFAULT_TEMPLATE_DIR
        )
        self.task_state_provider = task_state_provider or (lambda: {})
        self.trade_recipe_history = trade_recipe_history
        self._template_cache: dict[tuple[str, str], str] = {}

    def build(
        self,
        *,
        agent_id: str,
        teammates: Sequence[str],
        registry: SkillRegistry,
        last_error: str | None = None,
        error_history: Sequence[str] | None = None,
    ) -> LLMRequest:
        context = self.memory.to_prompt_context(
            agent_id,
            chat_window=self.config.chat_window or DEFAULT_TEAM_CHAT_WINDOW,
        )
        catalog = self._skill_catalog(registry)
        language = self.config.language
        system = self._template("system", language).format(
            objective=task_description_text(self.spec, agent_id=agent_id),
            catalog=catalog,
            protocol=self.codec.format_instructions(),
        )

        observation = context.get("observation")
        observation_text = (
            str(observation).strip() if observation is not None else "尚无有效观测。"
        )
        history_text = render_history(
            filter_trade_recipe_history(context.get("history") or []),
            instructions=(
                self.instructions
                if getattr(self.spec, "category", "") == "trade"
                else None
            ),
        )
        team = "、".join(teammates) if teammates else "无"
        error_text = _render_error_history(error_history, last_error)

        user = self._template("user", language).format(
            agent_id=agent_id,
            team=team,
            observation_text=observation_text,
            history_text=history_text,
            error_text=error_text,
        )
        messages = append_trade_recipe_section(
            (
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ),
            spec=self.spec,
            agent_id=agent_id,
            history=self.trade_recipe_history,
            history_entries=context.get("history") or (),
        )
        return LLMRequest(
            messages=messages,
            metadata={
                "method": "ppa",
                "task_id": self.spec.task_id,
                "agent_id": agent_id,
            },
            source=LLMCallSource(
                component="planning_worker",
                operation="plan",
                task_id=self.spec.task_id,
                agent_id=agent_id,
            ),
        )

    def build_passive(
        self,
        *,
        agent_id: str,
        teammates: Sequence[str],
        last_action_result: str,
    ) -> LLMRequest:
        """Build a passive-communication request producing a plain-text team message.

        Input is mostly the same as planning (task, state, observation) plus the
        previous action-result feedback; only the output guidance differs.
        """
        context = self.memory.to_prompt_context(
            agent_id,
            chat_window=self.config.chat_window or DEFAULT_TEAM_CHAT_WINDOW,
        )
        language = self.config.language
        system = self._template("passive_system", language).format(
            objective=task_description_text(self.spec, agent_id=agent_id),
        )
        observation = context.get("observation")
        observation_text = (
            str(observation).strip() if observation is not None else "尚无有效观测。"
        )
        history_text = render_history(
            filter_trade_recipe_history(context.get("history") or []),
            instructions=(
                self.instructions
                if getattr(self.spec, "category", "") == "trade"
                else None
            ),
        )
        team = "、".join(teammates) if teammates else "无"
        user = self._template("passive_user", language).format(
            agent_id=agent_id,
            team=team,
            observation_text=observation_text,
            history_text=history_text,
            last_action_result=last_action_result or "暂无动作结果。",
        )
        messages = append_trade_recipe_section(
            (
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ),
            spec=self.spec,
            agent_id=agent_id,
            history=self.trade_recipe_history,
            history_entries=context.get("history") or (),
        )
        return LLMRequest(
            messages=messages,
            metadata={
                "method": "ppa_passive",
                "task_id": self.spec.task_id,
                "agent_id": agent_id,
            },
            source=LLMCallSource(
                component="planning_worker",
                operation="passive_communication",
                task_id=self.spec.task_id,
                agent_id=agent_id,
            ),
        )

    def _template(self, name: str, language: str) -> str:
        """Load ``<name>_<language>.txt`` from the template dir, falling back to ``<name>.txt``."""
        key = (name, language)
        cached = self._template_cache.get(key)
        if cached is not None:
            return cached
        candidates = (
            self.template_dir / f"{name}_{language}.txt",
            self.template_dir / f"{name}.txt",
            DEFAULT_TEMPLATE_DIR / f"{name}_{language}.txt",
            DEFAULT_TEMPLATE_DIR / f"{name}.txt",
        )
        path = next(
            (candidate for candidate in candidates if candidate.is_file()),
            None,
        )
        if path is None:
            raise FileNotFoundError(
                f"prompt template {name!r} not found under {self.template_dir}"
            )
        text = path.read_text(encoding="utf-8")
        self._template_cache[key] = text
        return text


    def _skill_catalog(self, registry: SkillRegistry) -> str:
        return render_action_space(
            registry,
            self.instructions,
            self.config.language,
        )
