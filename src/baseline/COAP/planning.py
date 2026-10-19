"""COAP 缓存相关的同步 LLM 端口与响应解析。

模型客户端通过 ``LLMClientPort`` 注入；组装阶段将 PPA 的 LLM client
包装为该端口即可复用。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .prompts import COAPPromptBuilder
from .schemas import (
    Action,
    ActionSpec,
    CacheContentResult,
    CacheEntry,
    CacheInitializationBatch,
    CacheReason,
    CacheSource,
    TaskContext,
)


class PlanningError(RuntimeError):
    pass


class PlanningPromptError(PlanningError):
    pass


class PlanningResponseError(PlanningError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    """一次同步 LLM 调用的最小响应契约。"""

    content: str
    request_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLMClientPort(Protocol):
    """同步 LLM 客户端端口（组装时由 PPA LLM client 适配）。"""

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        metadata: dict[str, Any],
    ) -> LLMResponse:
        ...

_CACHE_BOOT_ACTION_NAME_KEY = "cache_boot_action_name"


def _parse_object(
    text: str,
    *,
    operation: str,
    required: set[str],
    allowed: set[str],
    allow_markdown_fences: bool,
) -> dict[str, Any]:
    source = (
        _strip_outer_code_fence(text) if allow_markdown_fences else text.strip()
    )
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise PlanningResponseError(
            f"{operation} response is not one complete JSON object: {exc.msg}",
        ) from exc
    if not isinstance(value, dict):
        raise PlanningResponseError(f"{operation} response must be a JSON object")
    missing = required - value.keys()
    if missing:
        raise PlanningResponseError(
            f"{operation} response is missing fields: " + ", ".join(sorted(missing))
        )
    unknown = value.keys() - allowed
    if unknown:
        raise PlanningResponseError(
            f"{operation} response has unknown fields: "
            + ", ".join(sorted(unknown))
        )
    return value


def _bounded_text(value: Any, field_path: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlanningResponseError(f"{field_path} must be a string")
    text = value.strip()
    if not text:
        raise PlanningResponseError(f"{field_path} must not be empty")
    if len(text) > maximum:
        raise PlanningResponseError(f"{field_path} exceeds the configured character limit")
    return text


def _parse_action_value(value: Any, field_path: str) -> Action:
    if not isinstance(value, dict):
        raise PlanningResponseError(f"{field_path} must be a JSON object")
    allowed = {"name", "parameters"}
    missing = {"name"} - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise PlanningResponseError(f"{field_path} is missing name")
    if unknown:
        raise PlanningResponseError(
            f"{field_path} has unknown fields: " + ", ".join(sorted(unknown))
        )
    name = value["name"]
    if not isinstance(name, str) or not name.strip():
        raise PlanningResponseError(f"{field_path}.name must be a non-empty string")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict):
        raise PlanningResponseError(
            f"{field_path}.parameters must be a JSON object"
        )
    return Action(name=name, parameters=dict(parameters))


def _parse_boot_action_name(
    value: Any,
    field_path: str,
    *,
    action_aliases: Mapping[str, str] | None,
    forbidden_names: set[str],
) -> Action:
    if not isinstance(value, str):
        raise PlanningResponseError(f"{field_path} must be an action name string")
    name = value.strip()
    if not name:
        raise PlanningResponseError(f"{field_path} must not be empty")
    if any(marker in name for marker in ("(", ")", "{", "}", "[", "]")):
        raise PlanningResponseError(f"{field_path} must contain only an action name")
    if "\n" in name or "\r" in name:
        raise PlanningResponseError(f"{field_path} must be one action name")
    if name in forbidden_names:
        raise PlanningResponseError(f"{field_path} must not use {name}")
    if action_aliases is not None:
        canonical = action_aliases.get(name)
        if canonical is None:
            raise PlanningResponseError(f"{field_path} is not in the action space")
    else:
        canonical = name
    return Action(name=canonical, parameters={})


def _reject_json_or_multiline(text: str, field_path: str) -> None:
    if "\n" in text or "\r" in text:
        raise PlanningResponseError(f"{field_path} must be one plain-text paragraph")
    if text.startswith("```"):
        raise PlanningResponseError(f"{field_path} must not use markdown fences")
    try:
        json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        raise PlanningResponseError(f"{field_path} must be plain text, not JSON")


def _strip_outer_code_fence(text: str) -> str:
    source = text.strip()
    lines = source.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return source


def _recover_boot_entry_objects(
    source: str,
) -> tuple[list[dict[str, Any]], int]:
    """从损坏的 entries 数组中尽量提取仍可独立解析的对象。"""
    match = re.search(r'"entries"\s*:', source)
    if match is None:
        return [], 0
    array_start = source.find("[", match.end())
    if array_start < 0:
        return [], 0

    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    in_string = False
    escaped = False
    array_depth = 1
    index = array_start + 1
    malformed_count = 0

    def object_end(start: int) -> int:
        depth = 0
        quoted = False
        escaped_value = False
        for offset in range(start, len(source)):
            value = source[offset]
            if quoted:
                if escaped_value:
                    escaped_value = False
                elif value == "\\":
                    escaped_value = True
                elif value == '"':
                    quoted = False
                continue
            if value == '"':
                quoted = True
            elif value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
                if depth == 0:
                    return offset + 1
        return len(source)

    while index < len(source) and array_depth > 0:
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if character == "[":
            array_depth += 1
            index += 1
            continue
        if character == "]":
            array_depth -= 1
            index += 1
            continue
        if array_depth == 1 and character == "{":
            try:
                value, end = decoder.raw_decode(source, index)
            except json.JSONDecodeError:
                malformed_count += 1
                end = object_end(index)
                index = end if end > index else index + 1
                continue
            if isinstance(value, dict):
                objects.append(value)
            index = end
            continue
        index += 1
    return objects, malformed_count


class CacheBootParser:
    """解析缓存初始化响应 {"entries": [...]}。"""

    def __init__(
        self,
        *,
        allow_markdown_fences: bool = True,
        maximum_reasoning_characters: int = 2000,
    ) -> None:
        self.allow_markdown_fences = allow_markdown_fences
        self.maximum_reasoning_characters = maximum_reasoning_characters

    def parse(
        self,
        text: str,
        *,
        maximum_entries: int,
        response: LLMResponse,
        action_aliases: Mapping[str, str] | None = None,
        forbidden_action_names: set[str] | None = None,
        forbidden_previous_action_names: set[str] | None = None,
        forbidden_next_action_names: set[str] | None = None,
    ) -> CacheInitializationBatch:
        source = (
            _strip_outer_code_fence(text)
            if self.allow_markdown_fences
            else text.strip()
        )
        recovery_mode = False
        try:
            value = _parse_object(
                source,
                operation="cache_boot",
                required={"entries"},
                allowed={"entries"},
                allow_markdown_fences=False,
            )
            entries_data = value["entries"]
            if not isinstance(entries_data, list):
                raise PlanningResponseError("cache_boot.entries must be an array")
        except PlanningResponseError:
            recovery_mode = True
            entries_data, malformed_count = _recover_boot_entry_objects(source)
        entries: list[CacheEntry] = []
        dropped_entries: list[dict[str, Any]] = []
        if recovery_mode:
            dropped_entries.extend(
                {
                    "position": None,
                    "reason": "entry object could not be decoded",
                }
                for _ in range(malformed_count)
            )
        forbidden = set(forbidden_action_names or ())
        forbidden_previous = forbidden | set(forbidden_previous_action_names or ())
        forbidden_next = forbidden | set(forbidden_next_action_names or ())
        seen_indexes: set[int] = set()
        for position, item in enumerate(entries_data):
            try:
                entry = self._parse_entry(
                    item,
                    position=position,
                    maximum_entries=maximum_entries,
                    action_aliases=action_aliases,
                    forbidden_previous_names=forbidden_previous,
                    forbidden_next_names=forbidden_next,
                )
                entry_index = item["index"]
                if entry_index in seen_indexes:
                    raise PlanningResponseError(
                        f"entries[{position}].index is duplicated"
                    )
                seen_indexes.add(entry_index)
                entries.append(entry)
            except (PlanningResponseError, TypeError, ValueError) as exc:
                recovery_mode = True
                dropped_entries.append(
                    {"position": position, "reason": str(exc)}
                )
        return CacheInitializationBatch(
            entries=tuple(entries),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            complete=(
                not recovery_mode
                and len(entries) == maximum_entries
            ),
            metadata={
                "request_id": response.request_id,
                "model_name": response.model,
                "accepted_entry_count": len(entries),
                "dropped_entry_count": len(dropped_entries),
                "dropped_entries": dropped_entries,
                "recovery_mode": recovery_mode,
            },
        )

    def _parse_entry(
        self,
        item: Any,
        *,
        position: int,
        maximum_entries: int,
        action_aliases: Mapping[str, str] | None,
        forbidden_previous_names: set[str],
        forbidden_next_names: set[str],
    ) -> CacheEntry:
        if not isinstance(item, dict):
            raise PlanningResponseError(
                f"cache_boot.entries[{position}] must be an object"
            )
        required = {
            "index",
            "previous_action",
            "next_action",
            "reason",
            "initial_score",
        }
        unknown = item.keys() - required
        if required - item.keys() or unknown:
            raise PlanningResponseError(
                f"cache_boot.entries[{position}] has invalid fields"
            )
        entry_index = item["index"]
        if (
            isinstance(entry_index, bool)
            or not isinstance(entry_index, int)
            or not 1 <= entry_index <= maximum_entries
        ):
            raise PlanningResponseError(
                f"cache_boot.entries[{position}].index must be within "
                f"[1,{maximum_entries}]"
            )
        reason = _bounded_text(
            item["reason"],
            f"entries[{position}].reason",
            self.maximum_reasoning_characters,
        )
        _reject_json_or_multiline(reason, f"entries[{position}].reason")
        score = item["initial_score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 < float(score) <= 0.5
        ):
            raise PlanningResponseError(
                f"entries[{position}].initial_score must be within (0,0.5]"
            )
        previous_action = _parse_boot_action_name(
            item["previous_action"],
            f"entries[{position}].previous_action",
            action_aliases=action_aliases,
            forbidden_names=forbidden_previous_names,
        )
        next_action = _parse_boot_action_name(
            item["next_action"],
            f"entries[{position}].next_action",
            action_aliases=action_aliases,
            forbidden_names=forbidden_next_names,
        )
        return CacheEntry(
            previous_action=previous_action,
            next_action=next_action,
            reason=reason,
            score=float(score),
            source=CacheSource.LLM,
            metadata={"llm_initial_score": float(score)},
        )


class CacheContentParser:
    """解析缓存内容更新响应 {"reason": ..., "initial_score": ...}。"""

    def __init__(
        self,
        *,
        allow_markdown_fences: bool = True,
        maximum_reasoning_characters: int = 2000,
    ) -> None:
        self.allow_markdown_fences = allow_markdown_fences
        self.maximum_reasoning_characters = maximum_reasoning_characters

    def parse(
        self,
        text: str,
        *,
        require_initial_score: bool,
        response: LLMResponse,
    ) -> CacheContentResult:
        required_fields = {"reason", "initial_score"} if require_initial_score else {"reason"}
        value = _parse_object(
            text,
            operation="cache_content_update",
            required=required_fields,
            allowed={"reason", "initial_score"},
            allow_markdown_fences=self.allow_markdown_fences,
        )
        score = value.get("initial_score")
        if require_initial_score:
            if score is None:
                raise PlanningResponseError(
                    "initial_score is required for a new cache pair"
                )
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 < float(score) <= 0.5
            ):
                raise PlanningResponseError(
                    "initial_score must be within (0,0.5]"
                )
        reason = _bounded_text(
            value["reason"],
            "reason",
            self.maximum_reasoning_characters,
        )
        _reject_json_or_multiline(reason, "reason")
        return CacheContentResult(
            reason=reason,
            initial_score=float(score) if require_initial_score else None,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            model_name=response.model,
            metadata={"request_id": response.request_id},
        )


class ModelCacheInitializer:
    """基于 LLM 的缓存初始化端口实现（同步）。"""

    def __init__(
        self,
        client: LLMClientPort,
        builder: COAPPromptBuilder,
        parser: CacheBootParser,
    ) -> None:
        self._client = client
        self._builder = builder
        self._parser = parser

    def initialize(
        self,
        task_context: TaskContext,
        action_space: Sequence[ActionSpec],
        *,
        maximum_entries: int,
        remaining_token_budget: int,
    ) -> CacheInitializationBatch:
        if isinstance(maximum_entries, bool) or maximum_entries < 1:
            raise PlanningPromptError("maximum_entries must be positive")
        if isinstance(remaining_token_budget, bool) or remaining_token_budget < 1:
            raise PlanningPromptError("remaining_token_budget must be positive")
        if isinstance(action_space, (str, bytes)):
            raise PlanningPromptError("action_space must be a sequence of ActionSpec")
        action_aliases = _action_aliases(action_space)
        messages = self._builder.build_cache_boot(
            objective=_goal_text(task_context),
            action_space=_action_space_text(action_space),
            maximum_entries=maximum_entries,
        )
        response = self._client.complete(
            messages,
            metadata={
                "operation": "cache_boot",
                "agent_id": "shared",
                "task_id": task_context.task_id,
                "maximum_entries": maximum_entries,
            },
        )
        return self._parser.parse(
            response.content,
            maximum_entries=maximum_entries,
            response=response,
            action_aliases=action_aliases,
        )


class ModelCacheContentGenerator:
    """基于 LLM 的缓存内容生成端口实现（同步）。"""

    def __init__(
        self,
        client: LLMClientPort,
        builder: COAPPromptBuilder,
        parser: CacheContentParser,
        *,
        task_context: TaskContext | None = None,
        action_space: Sequence[ActionSpec] | None = None,
    ) -> None:
        self._client = client
        self._builder = builder
        self._parser = parser
        self._objective = ""
        self._canonical_to_alias: dict[str, str] = {}
        self._aliases_to_canonical: dict[str, str] = {}
        self._action_descriptions: dict[str, str] = {}
        if task_context is not None:
            self.configure_context(task_context, action_space=action_space)

    def configure_context(
        self,
        task_context: TaskContext,
        *,
        action_space: Sequence[ActionSpec] | None = None,
    ) -> None:
        """配置内容更新 prompt 需要的任务背景与动作简写信息。"""
        if not isinstance(task_context, TaskContext):
            raise PlanningPromptError("task_context must be a TaskContext")
        resolved_action_space = (
            tuple(action_space)
            if action_space is not None
            else tuple(task_context.action_space)
        )
        if isinstance(resolved_action_space, (str, bytes)):
            raise PlanningPromptError("action_space must be a sequence of ActionSpec")
        if not all(isinstance(spec, ActionSpec) for spec in resolved_action_space):
            raise PlanningPromptError("action_space must contain ActionSpec")
        self._objective = _goal_text(task_context)
        self._canonical_to_alias = _canonical_action_aliases(resolved_action_space)
        self._aliases_to_canonical = _action_aliases(resolved_action_space)
        self._action_descriptions = {
            spec.name: spec.description.strip()
            for spec in resolved_action_space
            if isinstance(spec.description, str) and spec.description.strip()
        }

    def generate(
        self,
        previous_action: Action,
        next_action: Action,
        reasons: Sequence[str],
        *,
        existing_reason: CacheReason | None = None,
        require_initial_score: bool = False,
        owner_agent_id: str | None = None,
    ) -> CacheContentResult:
        if isinstance(reasons, (str, bytes)) or not reasons or not all(
            isinstance(reason, str) and reason.strip() for reason in reasons
        ):
            raise PlanningPromptError("reasons must contain non-empty strings")
        previous_alias = self._action_alias(previous_action)
        next_alias = self._action_alias(next_action)
        messages = self._builder.build_cache_content(
            objective=self._objective,
            previous_action=previous_alias,
            next_action=next_alias,
            previous_action_description=self._action_description(previous_action),
            next_action_description=self._action_description(next_action),
            reasons=reasons,
            existing_reason=existing_reason,
            require_initial_score=require_initial_score,
        )
        response = self._client.complete(
            messages,
            metadata={
                "operation": "cache_content_update",
                "agent_id": owner_agent_id or "shared",
                "previous_action": previous_alias,
                "next_action": next_alias,
                "require_initial_score": require_initial_score,
            },
        )
        return self._parser.parse(
            response.content,
            require_initial_score=require_initial_score,
            response=response,
        )

    def _action_alias(self, action: Action) -> str:
        value = action.metadata.get(_CACHE_BOOT_ACTION_NAME_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self._canonical_to_alias.get(action.name, action.name)

    def _action_description(self, action: Action) -> str:
        canonical = self._aliases_to_canonical.get(action.name, action.name)
        return self._action_descriptions.get(canonical, "")


def _goal_text(task_context: TaskContext) -> str:
    goal = task_context.goal
    if isinstance(goal, str):
        return goal
    try:
        return json.dumps(goal, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(goal)


def _action_space_text(action_space: Sequence[ActionSpec]) -> str:
    lines = []
    for spec in action_space:
        lines.append(f"- {_cache_boot_action_name(spec)}")
    return "\n".join(lines) if lines else "（空动作空间）"


def _cache_boot_action_name(spec: ActionSpec) -> str:
    value = spec.metadata.get(_CACHE_BOOT_ACTION_NAME_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return spec.name


def _action_aliases(action_space: Sequence[ActionSpec]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for spec in action_space:
        aliases[spec.name] = spec.name
        aliases[_cache_boot_action_name(spec)] = spec.name
    return aliases


def _canonical_action_aliases(action_space: Sequence[ActionSpec]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for spec in action_space:
        alias = _cache_boot_action_name(spec)
        aliases[spec.name] = alias
        aliases[alias] = alias
    return aliases


__all__ = [
    "CacheBootParser",
    "CacheContentParser",
    "LLMClientPort",
    "LLMResponse",
    "ModelCacheContentGenerator",
    "ModelCacheInitializer",
    "PlanningError",
    "PlanningPromptError",
    "PlanningResponseError",
]
