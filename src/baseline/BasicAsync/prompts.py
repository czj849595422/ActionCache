"""Prompt construction for the BasicAsync baseline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Sequence

from src.baseline.COAP.protocol import (
    DEFAULT_ACTION_READY_KEYWORD,
    format_action_instructions,
)


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompts"
ActionOperation = Literal["standard_action", "direct_action"]


class BasicAsyncPromptBuilder:
    """Build BasicAsync action prompts from BasicAsync-owned templates."""

    default_template_dir = DEFAULT_TEMPLATE_DIR

    def __init__(
        self,
        *,
        template_dir: str | Path | None = None,
        language: str = "zh",
        action_ready_keyword: str = DEFAULT_ACTION_READY_KEYWORD,
    ) -> None:
        if language not in {"zh", "en"}:
            raise ValueError("language 必须是 'zh' 或 'en'")
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else self.default_template_dir
        )
        self.language = language
        self.action_ready_keyword = action_ready_keyword
        self._template_cache: dict[str, str] = {}

    def build_action(
        self,
        *,
        operation: ActionOperation,
        objective: str,
        catalog: str,
        agent_id: str,
        team: str,
        observation_text: str,
        history_text: str,
        error_text: str,
        feedback_text: str = "",
        cache_candidates: Sequence[Any] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Build a standard or direct action request.

        ``feedback_text`` and ``cache_candidates`` remain accepted so the
        request contract is stable, but BasicAsync does not render COAP cache
        state into its prompt.
        """
        del cache_candidates
        system = self._template("system").format(
            objective=objective or "",
            catalog=catalog or "",
            protocol=format_action_instructions(operation),
        )
        user = self._template("user").format(
            agent_id=agent_id,
            team=team or "无",
            observation_text=observation_text or "尚无有效观测。",
            history_text=history_text or "暂无历史信息。",
            error_text=error_text or "无",
            feedback_section=feedback_text or "",
        )
        return (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )

    def _template(self, name: str) -> str:
        cached = self._template_cache.get(name)
        if cached is not None:
            return cached
        path = self.template_dir / f"{name}.txt"
        if not path.is_file():
            raise FileNotFoundError(
                f"prompt template {name!r} not found under {self.template_dir}"
            )
        text = path.read_text(encoding="utf-8")
        self._template_cache[name] = text
        return text


__all__ = [
    "ActionOperation",
    "BasicAsyncPromptBuilder",
    "DEFAULT_ACTION_READY_KEYWORD",
    "DEFAULT_TEMPLATE_DIR",
]
