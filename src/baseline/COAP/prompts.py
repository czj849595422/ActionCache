"""COAP 缓存相关 prompt 构建（system/user 结构对齐当前 PPA）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Sequence

from .config import CacheActionCandidatePolicy
from .protocol import DEFAULT_ACTION_READY_KEYWORD, format_action_instructions
from .schemas import CacheReason


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompts"

ActionOperation = Literal["standard_action", "cache_action", "direct_action"]


class COAPPromptBuilder:
    """构建缓存相关 prompt 的 system/user 消息对。

    普通/缓存动作规划复用 PPA 的 system/user 结构，仅增加缓存候选段；
    缓存初始化与内容更新使用独立的简洁模板。
    """

    def __init__(
        self,
        *,
        template_dir: str | Path | None = None,
        language: str = "zh",
        action_ready_keyword: str = DEFAULT_ACTION_READY_KEYWORD,
        candidate_policy: CacheActionCandidatePolicy = (
            CacheActionCandidatePolicy.ADVISORY
        ),
    ) -> None:
        if language not in {"zh", "en"}:
            raise ValueError("language 必须是 'zh' 或 'en'")
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else DEFAULT_TEMPLATE_DIR
        )
        self.language = language
        self.action_ready_keyword = action_ready_keyword
        self.candidate_policy = CacheActionCandidatePolicy(candidate_policy)
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
        instructions: Any | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """构建普通/缓存动作规划消息。cache_candidates 仅缓存路径注入。"""
        protocol = self._format_action_protocol(operation)
        system = self._template("system").format(
            objective=objective or "",
            catalog=catalog or "",
            protocol=protocol,
        )
        cache_section = ""
        if operation == "cache_action":
            cache_section = self._cache_candidate_section(cache_candidates, instructions)
        user = self._template("user").format(
            agent_id=agent_id,
            team=team or "无",
            observation_text=observation_text or "尚无有效观测。",
            history_text=history_text or "暂无历史信息。",
            error_text=error_text or "无",
            feedback_section=feedback_text,
            cache_section=cache_section,
        )
        return (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )

    def build_cache_boot(
        self,
        *,
        objective: str,
        action_space: str,
        maximum_entries: int,
    ) -> tuple[dict[str, str], dict[str, str]]:
        system = self._template("cache_boot_system")
        user = self._template("cache_boot_user").format(
            objective=objective or "",
            action_space=action_space or "",
            maximum_entries=maximum_entries,
        )
        return (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )

    def build_cache_content(
        self,
        *,
        objective: str,
        previous_action: Any,
        next_action: Any,
        previous_action_description: str = "",
        next_action_description: str = "",
        reasons: Sequence[str],
        existing_reason: CacheReason | None = None,
        require_initial_score: bool = False,
        instructions: Any | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        score_instruction = (
            '"initial_score" 必须在 (0,0.5] 内。'
            if require_initial_score
            else '已有缓存对无需重新评分；"initial_score" 可为 null 或省略，即使返回评分也会被忽略。'
        )
        system = self._template("cache_content_system").format(
            score_instruction=score_instruction,
        )
        reason_lines = "\n".join(
            f"{index}. {reason}" for index, reason in enumerate(reasons, start=1)
        )
        existing_section = (
            ""
            if existing_reason is None
            else f"\n【现有原因】\n{existing_reason.text}"
        )
        user = self._template("cache_content_user").format(
            objective=objective or "",
            previous_action=_action_text(previous_action, instructions),
            next_action=_action_text(next_action, instructions),
            previous_action_description=previous_action_description or "无",
            next_action_description=next_action_description or "无",
            reasons=reason_lines or "（无候选原因）",
            existing_section=existing_section,
        )
        return (
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        )

    def _format_action_protocol(self, operation: ActionOperation) -> str:
        return format_action_instructions(
            operation,
            cache_candidate_instruction=(
                self._candidate_instruction()
                if operation == "cache_action"
                else ""
            ),
        )

    def _candidate_instruction(self) -> str:
        if self.candidate_policy is CacheActionCandidatePolicy.STRICT:
            return (
                " action.name 必须匹配一个候选缓存动作。"
                if self.language == "zh"
                else " action.name must match one supplied cache candidate."
            )
        return (
            "优先从候选缓存中选择动作；若所有候选不适合当前局部观测，"
            "可以选择动作空间中的其他动作。动作参数根据当前观测填写。"
            if self.language == "zh"
            else " Prefer an action from the cache candidates. If none fits the "
            "current local observation, you may choose another action from the "
            "action space. Fill parameters from the current observation."
        )

    def _cache_candidate_section(
        self,
        cache_candidates: Sequence[Any] | None,
        instructions: Any | None = None,
    ) -> str:
        if not cache_candidates:
            return "\n\n【缓存候选】\n（当前无可用缓存候选，请直接进行普通规划。）"
        lines = []
        for entry in cache_candidates:
            previous = _action_text(entry.previous_action, instructions)
            next_action = _action_text(entry.next_action, instructions)
            score = getattr(entry, "score", 0.0)
            reason = getattr(entry, "reason", "")
            lines.append(
                f"- 前序: {previous} -> 后继: {next_action} "
                f"(评分 {score:.2f}; 原因: {reason})"
            )
        return "\n\n【缓存候选】\n" + "\n".join(lines)

    def _template(self, name: str) -> str:
        cached = self._template_cache.get(name)
        if cached is not None:
            return cached
        path = self.template_dir / f"{name}.txt"
        if not path.is_file():
            fallback = DEFAULT_TEMPLATE_DIR / f"{name}.txt"
            if not fallback.is_file():
                raise FileNotFoundError(
                    f"prompt template {name!r} not found under {self.template_dir}"
                )
            path = fallback
        text = path.read_text(encoding="utf-8")
        self._template_cache[name] = text
        return text


def _action_text(action: Any, instructions: Any | None = None) -> str:
    name = getattr(action, "name", str(action))
    if instructions is not None and name != "无":
        resolver = getattr(instructions, "instruction_for_action", None)
        if callable(resolver):
            instruction = resolver(str(name))
            if instruction is not None:
                name = getattr(instruction, "alias", name) or name
    parameters = getattr(action, "parameters", None)
    if not parameters:
        return str(name)
    return f"{name}({parameters})"


__all__ = [
    "COAPPromptBuilder",
    "DEFAULT_ACTION_READY_KEYWORD",
    "DEFAULT_TEMPLATE_DIR",
]
