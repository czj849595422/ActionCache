"""AgenticCache baseline data contracts.

AgenticCache keeps its cache implementation local to this baseline.  The cache
stores high-level action transitions, not COAP score records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any
from uuid import uuid4


JsonValue = Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


def _require_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    value = float(value)
    if not 0 <= value <= 1:
        raise ValueError(f"{label} 必须在 [0, 1] 内")
    return value


def _require_positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    value = float(value)
    if value <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if value < minimum:
        raise ValueError(f"{label} 必须 >= {minimum}")
    return value


@dataclass(frozen=True)
class AgenticCacheConfig:
    """Runtime configuration for AgenticCache.

    The control flow follows BasicAsync: decide only after the previous action
    finishes.  Cache hits avoid an LLM call; cache misses call the model once.
    """

    language: str = "zh"
    observation_interval: float = 1.0
    acting_poll_interval: float = 0.2
    planning_poll_interval: float = 0.1
    task_poll_interval: float = 0.2
    initial_observation_timeout: float = 10.0
    worker_stop_timeout: float = 5.0
    chat_window: int | None = 20
    max_run_seconds: float | None = None
    max_llm_calls: int | None = None
    max_llm_calls_per_agent: int | None = None
    broadcast_results: bool = True
    enable_chat_actions: bool = False
    system_messages_enabled: bool = False
    system_message_interval: float = 30.0
    max_consecutive_errors: int = 3
    cache_top_k: int = 5
    metadata_tolerance: float = 0.0
    max_metadata_fields: int = 32
    min_transition_count: int = 1
    start_action_name: str = "START"
    cache_source_path: str | None = None

    def __post_init__(self) -> None:
        if self.language not in {"zh", "en"}:
            raise ValueError("language 必须是 'zh' 或 'en'")
        for label in (
            "observation_interval",
            "acting_poll_interval",
            "planning_poll_interval",
            "task_poll_interval",
            "initial_observation_timeout",
            "worker_stop_timeout",
        ):
            object.__setattr__(self, label, _require_positive(getattr(self, label), label))
        if self.chat_window is not None and self.chat_window < 1:
            raise ValueError("chat_window 必须为正数")
        if self.max_run_seconds is not None and self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds 必须为正数")
        if self.max_llm_calls is not None and self.max_llm_calls < 1:
            raise ValueError("max_llm_calls 必须 >= 1")
        if (
            self.max_llm_calls_per_agent is not None
            and self.max_llm_calls_per_agent < 1
        ):
            raise ValueError("max_llm_calls_per_agent 必须 >= 1")
        object.__setattr__(self, "cache_top_k", _require_int(self.cache_top_k, "cache_top_k", minimum=1))
        object.__setattr__(
            self,
            "max_metadata_fields",
            _require_int(self.max_metadata_fields, "max_metadata_fields", minimum=1),
        )
        object.__setattr__(
            self,
            "min_transition_count",
            _require_int(self.min_transition_count, "min_transition_count", minimum=1),
        )
        object.__setattr__(
            self,
            "metadata_tolerance",
            _require_probability(self.metadata_tolerance, "metadata_tolerance"),
        )
        object.__setattr__(
            self,
            "max_consecutive_errors",
            _require_int(self.max_consecutive_errors, "max_consecutive_errors", minimum=1),
        )
        if not isinstance(self.broadcast_results, bool):
            raise ValueError("broadcast_results 必须是布尔值")
        if not isinstance(self.enable_chat_actions, bool):
            raise ValueError("enable_chat_actions 必须是布尔值")
        if not isinstance(self.system_messages_enabled, bool):
            raise ValueError("system_messages_enabled 必须是布尔值")
        object.__setattr__(
            self,
            "system_message_interval",
            _require_positive(self.system_message_interval, "system_message_interval"),
        )
        if not isinstance(self.start_action_name, str) or not self.start_action_name.strip():
            raise ValueError("start_action_name 必须是非空字符串")


@dataclass
class ActionPlan:
    """Cache-level shorthand representation of one high-level action."""

    name: str
    parameters: dict[str, JsonValue] = field(default_factory=dict)
    input_positional: list[JsonValue] = field(default_factory=list)
    canonical_name: str | None = None
    canonical_parameters: dict[str, JsonValue] = field(default_factory=dict)
    action_text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name 必须是非空字符串")
        if not isinstance(self.parameters, dict):
            raise ValueError("parameters 必须是字典")
        if not isinstance(self.input_positional, list):
            raise ValueError("input_positional 必须是列表")
        if self.canonical_name is not None and not isinstance(self.canonical_name, str):
            raise ValueError("canonical_name 必须是字符串或 None")
        if not isinstance(self.canonical_parameters, dict):
            raise ValueError("canonical_parameters 必须是字典")
        if not isinstance(self.action_text, str):
            raise ValueError("action_text 必须是字符串")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "parameters": dict(self.parameters),
            "input_positional": list(self.input_positional),
            "canonical_name": self.canonical_name,
            "canonical_parameters": dict(self.canonical_parameters),
            "action_text": self.action_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionPlan":
        return cls(
            name=str(data.get("name") or ""),
            parameters=dict(data.get("parameters") or {}),
            input_positional=list(data.get("input_positional") or []),
            canonical_name=(
                None
                if data.get("canonical_name") is None
                else str(data.get("canonical_name"))
            ),
            canonical_parameters=dict(data.get("canonical_parameters") or {}),
            action_text=str(data.get("action_text") or ""),
        )


@dataclass
class MetadataRange:
    """Observed integer/float range for one task-state feature."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        if self.maximum < self.minimum:
            raise ValueError("metadata range maximum 不能小于 minimum")

    def contains(self, value: float, *, tolerance: float = 0.0) -> bool:
        span = max(1.0, abs(self.maximum - self.minimum))
        padding = span * max(0.0, tolerance)
        return self.minimum - padding <= value <= self.maximum + padding

    def include(self, value: float) -> None:
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def to_dict(self) -> dict[str, float]:
        return {"minimum": self.minimum, "maximum": self.maximum}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetadataRange":
        return cls(minimum=float(data["minimum"]), maximum=float(data["maximum"]))


@dataclass
class AgenticCacheEntry:
    """A 2-gram action transition with state-range metadata."""

    previous_action: ActionPlan
    next_action: ActionPlan
    metadata_ranges: dict[str, MetadataRange] = field(default_factory=dict)
    transition_count: int = 1
    candidate_count: int = 1
    confirmed_count: int = 1
    failure_count: int = 0
    reason: str = ""
    source: str = "online"
    owner_agent_id: str | None = None
    entry_id: str = field(default_factory=lambda: _new_id("ac"))
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.previous_action, ActionPlan):
            raise ValueError("previous_action 必须是 ActionPlan")
        if not isinstance(self.next_action, ActionPlan):
            raise ValueError("next_action 必须是 ActionPlan")
        self.transition_count = _require_int(self.transition_count, "transition_count")
        self.candidate_count = _require_int(self.candidate_count, "candidate_count")
        self.confirmed_count = _require_int(self.confirmed_count, "confirmed_count")
        self.failure_count = _require_int(self.failure_count, "failure_count")
        if not self.entry_id:
            raise ValueError("entry_id 必须非空")

    @property
    def importance(self) -> float:
        if self.candidate_count <= 0:
            return 0.0
        return self.confirmed_count / self.candidate_count

    @property
    def score(self) -> float:
        return float(self.transition_count) * self.importance

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "previous_action": self.previous_action.to_dict(),
            "next_action": self.next_action.to_dict(),
            "metadata_ranges": {
                key: value.to_dict() for key, value in sorted(self.metadata_ranges.items())
            },
            "transition_count": self.transition_count,
            "candidate_count": self.candidate_count,
            "confirmed_count": self.confirmed_count,
            "failure_count": self.failure_count,
            "importance": self.importance,
            "score": self.score,
            "reason": self.reason,
            "source": self.source,
            "owner_agent_id": self.owner_agent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgenticCacheEntry":
        return cls(
            entry_id=str(data.get("entry_id") or _new_id("ac")),
            previous_action=ActionPlan.from_dict(dict(data.get("previous_action") or {})),
            next_action=ActionPlan.from_dict(dict(data.get("next_action") or {})),
            metadata_ranges={
                str(key): MetadataRange.from_dict(dict(value))
                for key, value in dict(data.get("metadata_ranges") or {}).items()
            },
            transition_count=int(data.get("transition_count") or 0),
            candidate_count=int(data.get("candidate_count") or 0),
            confirmed_count=int(data.get("confirmed_count") or 0),
            failure_count=int(data.get("failure_count") or 0),
            reason=str(data.get("reason") or ""),
            source=str(data.get("source") or "prefill"),
            owner_agent_id=data.get("owner_agent_id"),
            created_at=float(data.get("created_at") or _now()),
            updated_at=float(data.get("updated_at") or _now()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class CacheLookupResult:
    previous_action: ActionPlan
    entries: tuple[AgenticCacheEntry, ...]
    matched_count: int
    filtered_count: int

    @property
    def hit(self) -> bool:
        return bool(self.entries)


@dataclass
class AgenticCacheProposal:
    """One executable proposal published from cache or LLM planning."""

    action_unit: Any
    reason: str = ""
    communication: str = ""
    path: str = "llm"
    previous_action: ActionPlan | None = None
    state_metadata: dict[str, float] = field(default_factory=dict)
    cache_entry_id: str | None = None
    plan_id: str = field(default_factory=lambda: _new_id("plan"))
    request_id: str | None = None
    raw_text: str = ""
    input_params: dict[str, Any] = field(default_factory=dict)
    input_positional: list[Any] = field(default_factory=list)
    cache_action: ActionPlan | None = None
    memory_entry_id: str | None = None
    communication_committed: bool = False
    planning_started_at: float | None = None
    planning_finished_at: float | None = None


@dataclass
class AgenticCacheRunResult:
    """Terminal result of one AgenticCache runner invocation."""

    outcome: Any | None = None
    final_metrics: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    stop_reason: str | None = None
    runner_error: str | None = None
    worker_errors: list[tuple[str, str]] = field(default_factory=list)
    cleanup_errors: list[tuple[str, str]] = field(default_factory=list)
    planned_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    llm_planned_count: int = 0
    executed_count: int = 0
    cache_stats: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def exit_code(self) -> int:
        from src.tasks import TaskStatus

        if self.interrupted:
            return 130
        if self.runner_error or self.worker_errors or self.cleanup_errors:
            return 1
        if self.outcome is None or self.outcome.status != TaskStatus.SUCCESS:
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "final_metrics": dict(self.final_metrics),
            "interrupted": self.interrupted,
            "stop_reason": self.stop_reason,
            "runner_error": self.runner_error,
            "worker_errors": [list(item) for item in self.worker_errors],
            "cleanup_errors": [list(item) for item in self.cleanup_errors],
            "planned_count": self.planned_count,
            "cache_hit_count": self.cache_hit_count,
            "cache_miss_count": self.cache_miss_count,
            "llm_planned_count": self.llm_planned_count,
            "executed_count": self.executed_count,
            "cache_stats": dict(self.cache_stats),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }


__all__ = [
    "ActionPlan",
    "AgenticCacheConfig",
    "AgenticCacheEntry",
    "AgenticCacheProposal",
    "AgenticCacheRunResult",
    "CacheLookupResult",
    "JsonValue",
    "MetadataRange",
]
