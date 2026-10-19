"""COAP 缓存模块数据契约（环境无关，同步版本）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any
from uuid import uuid4


JsonValue = Any


def _new_id() -> str:
    return uuid4().hex


def _now() -> float:
    return time.time()


def _coerce_enum(value: Any, enum_type: type[Enum], label: str):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{label} 非法: {value!r}") from exc


def _require_json(value: Any, label: str) -> None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_json(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} 的键必须是字符串")
            _require_json(item, f"{label}.{key}")
        return
    raise ValueError(f"{label} 必须是 JSON 值")


class CacheSource(str, Enum):
    LLM = "llm"
    ENV = "env"


class CandidateStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNCERTAIN = "uncertain"


@dataclass
class ActionSpec:
    """动作空间中的一条动作规格。"""

    name: str
    description: str = ""
    parameter_schema: dict[str, JsonValue] = field(default_factory=dict)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name 必须是非空字符串")
        if not isinstance(self.description, str):
            raise ValueError("description 必须是字符串")
        if not isinstance(self.parameter_schema, dict):
            raise ValueError("parameter_schema 必须是字典")
        _require_json(self.parameter_schema, "parameter_schema")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class Action:
    """环境无关的动作表示：动作名 + 参数字典。"""

    name: str
    parameters: dict[str, JsonValue] = field(default_factory=dict)
    agent_id: str | None = None
    canonical_key: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name 必须是非空字符串")
        if not isinstance(self.parameters, dict):
            raise ValueError("parameters 必须是字典")
        _require_json(self.parameters, "parameters")
        if self.agent_id is not None and (
            not isinstance(self.agent_id, str) or not self.agent_id.strip()
        ):
            raise ValueError("agent_id 必须是非空字符串或 None")
        if self.canonical_key is not None and (
            not isinstance(self.canonical_key, str) or not self.canonical_key.strip()
        ):
            raise ValueError("canonical_key 必须是非空字符串或 None")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class CacheReason:
    """一条动作转移的抽象原因。"""

    text: str
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text 必须是非空字符串")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")

    def __str__(self) -> str:
        return self.text


@dataclass
class CacheEntry:
    """一条动作转移缓存：前序动作 -> 后继动作。"""

    previous_action: Action
    next_action: Action
    reason: CacheReason | str
    entry_id: str = field(default_factory=_new_id)
    score: float = 0.5
    source: CacheSource = CacheSource.LLM
    hit_count: int = 0
    attempt_count: int = 0
    success_count: int = 0
    delta_attempt_count: int = 0
    delta_success_count: int = 0
    delta_hit_count: int = 0
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    last_hit_at: float | None = None
    task_scope: str | None = None
    owner_agent_id: str | None = None
    seed_entry_id: str | None = None
    initialization_batch_id: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.previous_action, Action):
            raise ValueError("previous_action 必须是 Action")
        if not isinstance(self.next_action, Action):
            raise ValueError("next_action 必须是 Action")
        if isinstance(self.reason, str):
            self.reason = CacheReason(self.reason)
        if not isinstance(self.reason, CacheReason):
            raise ValueError("reason 必须是 CacheReason 或文本")
        if not isinstance(self.entry_id, str) or not self.entry_id:
            raise ValueError("entry_id 必须是非空字符串")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("score 必须是数字")
        self.score = float(self.score)
        if not 0 <= self.score <= 1:
            raise ValueError("score 必须在 [0, 1] 内")
        self.source = _coerce_enum(self.source, CacheSource, "source")
        for name in (
            "hit_count",
            "attempt_count",
            "success_count",
            "delta_attempt_count",
            "delta_success_count",
            "delta_hit_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.success_count > self.attempt_count:
            raise ValueError("success_count 不能超过 attempt_count")
        if self.delta_success_count > self.delta_attempt_count:
            raise ValueError("delta_success_count 不能超过 delta_attempt_count")
        if isinstance(self.created_at, bool) or not isinstance(
            self.created_at, (int, float)
        ):
            raise ValueError("created_at 必须是数字")
        if isinstance(self.updated_at, bool) or not isinstance(
            self.updated_at, (int, float)
        ):
            raise ValueError("updated_at 必须是数字")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if self.last_hit_at is not None and (
            isinstance(self.last_hit_at, bool)
            or not isinstance(self.last_hit_at, (int, float))
        ):
            raise ValueError("last_hit_at 必须是数字或 None")
        for name in ("task_scope", "owner_agent_id", "seed_entry_id", "initialization_batch_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} 必须是非空字符串或 None")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")

    @property
    def success_rate(self) -> float | None:
        if self.attempt_count == 0:
            return None
        return self.success_count / self.attempt_count


@dataclass
class CacheSourceBundle:
    """从文件或目录清单加载的缓存源。"""

    layout: str
    shared_entries: tuple[CacheEntry, ...] = ()
    agent_entries: dict[str, tuple[CacheEntry, ...]] = field(default_factory=dict)
    source_path: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.layout not in {"shared", "per_agent"}:
            raise ValueError("layout 必须是 shared 或 per_agent")
        self.shared_entries = tuple(self.shared_entries)
        if not all(isinstance(item, CacheEntry) for item in self.shared_entries):
            raise ValueError("shared_entries 必须包含 CacheEntry")
        normalized: dict[str, tuple[CacheEntry, ...]] = {}
        for agent_id, entries in self.agent_entries.items():
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("agent_entries 键必须是非空字符串")
            values = tuple(entries)
            if not all(isinstance(item, CacheEntry) for item in values):
                raise ValueError("agent_entries 值必须包含 CacheEntry")
            normalized[agent_id] = values
        self.agent_entries = normalized
        if self.layout == "shared" and normalized:
            raise ValueError("shared 缓存源不能包含 agent_entries")
        if self.layout == "per_agent" and self.shared_entries:
            raise ValueError("per_agent 缓存源不能包含 shared_entries")


@dataclass
class CacheContentResult:
    """LLM 生成的一条缓存内容（reason 与可选初始评分）。"""

    reason: CacheReason | str
    initial_score: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model_name: str | None = None
    raw_response: JsonValue = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.reason, str):
            self.reason = CacheReason(self.reason)
        if not isinstance(self.reason, CacheReason):
            raise ValueError("reason 必须是 CacheReason 或文本")
        if self.initial_score is not None:
            if isinstance(self.initial_score, bool) or not isinstance(
                self.initial_score, (int, float)
            ):
                raise ValueError("initial_score 必须是数字或 None")
            self.initial_score = float(self.initial_score)
            if not 0 < self.initial_score <= 0.5:
                raise ValueError("initial_score 必须在 (0, 0.5] 内")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class CacheInitializationBatch:
    """一次有界的 LLM 缓存初始化响应。"""

    entries: tuple[CacheEntry, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    complete: bool = False
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.entries, (str, bytes)) or not isinstance(
            self.entries, (tuple, list)
        ):
            raise ValueError("entries 必须是序列")
        self.entries = tuple(self.entries)
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, CacheEntry):
                raise ValueError("entries 必须包含 CacheEntry")
            if entry.entry_id in seen:
                raise ValueError("entries 的 entry_id 必须唯一")
            seen.add(entry.entry_id)
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not isinstance(self.complete, bool):
            raise ValueError("complete 必须是布尔值")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class CandidateRecord:
    """普通规划暂存的一条候选记录（动作转移 + 状态）。"""

    decision_id: str
    agent_id: str
    previous_action: Action
    next_action: Action
    reasoning: str
    status: CandidateStatus
    record_id: str = field(default_factory=_new_id)
    plan_id: str | None = None
    cache_statistics_applied: bool = False
    created_at: float = field(default_factory=_now)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("decision_id", "agent_id", "record_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} 必须是非空字符串")
        if self.plan_id is not None and (
            not isinstance(self.plan_id, str) or not self.plan_id
        ):
            raise ValueError("plan_id 必须是非空字符串或 None")
        if not isinstance(self.previous_action, Action):
            raise ValueError("previous_action 必须是 Action")
        if not isinstance(self.next_action, Action):
            raise ValueError("next_action 必须是 Action")
        if not isinstance(self.reasoning, str):
            raise ValueError("reasoning 必须是字符串")
        self.status = _coerce_enum(self.status, CandidateStatus, "status")
        if not isinstance(self.cache_statistics_applied, bool):
            raise ValueError("cache_statistics_applied 必须是布尔值")
        if self.status is CandidateStatus.UNCERTAIN and self.cache_statistics_applied:
            raise ValueError("uncertain 候选不能已应用缓存统计")
        if isinstance(self.created_at, bool) or not isinstance(
            self.created_at, (int, float)
        ):
            raise ValueError("created_at 必须是数字")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class AggregatedCandidate:
    """按 (previous_action, next_action) 聚合后的候选。"""

    previous_action: Action
    next_action: Action
    success_count: int
    failure_count: int
    uncertain_count: int
    reasons: tuple[str, ...]
    record_ids: tuple[str, ...]
    first_created_at: float
    last_created_at: float
    pending_attempt_count: int = 0
    pending_success_count: int = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.previous_action, Action):
            raise ValueError("previous_action 必须是 Action")
        if not isinstance(self.next_action, Action):
            raise ValueError("next_action 必须是 Action")
        for name in (
            "success_count",
            "failure_count",
            "uncertain_count",
            "pending_attempt_count",
            "pending_success_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.pending_success_count > self.pending_attempt_count:
            raise ValueError("pending_success_count 不能超过 pending_attempt_count")
        if self.pending_attempt_count > self.success_count + self.failure_count:
            raise ValueError("pending_attempt_count 不能超过已评估记录数")
        if self.pending_success_count > self.success_count:
            raise ValueError("pending_success_count 不能超过 success_count")
        if not isinstance(self.reasons, (tuple, list)) or not all(
            isinstance(item, str) for item in self.reasons
        ):
            raise ValueError("reasons 必须是字符串元组")
        self.reasons = tuple(self.reasons)
        if not isinstance(self.record_ids, (tuple, list)) or not all(
            isinstance(item, str) for item in self.record_ids
        ):
            raise ValueError("record_ids 必须是字符串元组")
        self.record_ids = tuple(self.record_ids)
        total = self.success_count + self.failure_count + self.uncertain_count
        if total == 0:
            raise ValueError("聚合候选必须包含记录")
        if len(self.record_ids) != total:
            raise ValueError("record_ids 数量必须与状态计数一致")
        if self.last_created_at < self.first_created_at:
            raise ValueError("last_created_at 不能早于 first_created_at")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass
class TaskContext:
    """缓存初始化使用的任务上下文。"""

    task_id: str
    goal: JsonValue
    constraints: tuple[str, ...] = ()
    action_space: tuple[ActionSpec, ...] = ()
    history: tuple[JsonValue, ...] = ()
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id 必须是非空字符串")
        _require_json(self.goal, "goal")
        if not isinstance(self.constraints, (tuple, list)) or not all(
            isinstance(item, str) for item in self.constraints
        ):
            raise ValueError("constraints 必须是字符串元组")
        self.constraints = tuple(self.constraints)
        if not isinstance(self.action_space, (tuple, list)) or not all(
            isinstance(item, ActionSpec) for item in self.action_space
        ):
            raise ValueError("action_space 必须包含 ActionSpec")
        self.action_space = tuple(self.action_space)
        names = [item.name for item in self.action_space]
        if len(names) != len(set(names)):
            raise ValueError("action_space 动作名必须唯一")
        if not isinstance(self.history, (tuple, list)):
            raise ValueError("history 必须是元组")
        self.history = tuple(self.history)
        _require_json(self.history, "history")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是字典")


@dataclass(frozen=True)
class CacheBootResult:
    mode: str
    entries: tuple[CacheEntry, ...]
    created_count: int
    reused_count: int
    filtered_count: int
    duplicate_count: int
    llm_call_count: int
    input_tokens: int
    output_tokens: int
    changes: tuple["CacheEntryChange", ...] = ()


@dataclass(frozen=True)
class CacheLookupResult:
    previous_key: str
    entries: tuple[CacheEntry, ...]
    matched_count: int
    filtered_count: int

    @property
    def hit(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class BufferAddResult:
    accepted: bool
    duplicate: bool
    size: int
    should_flush: bool
    dropped_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheUpdateReport:
    entries: tuple[CacheEntry, ...]
    created_entry_ids: tuple[str, ...]
    updated_entry_ids: tuple[str, ...]
    removed_duplicate_ids: tuple[str, ...]
    processed_record_ids: tuple[str, ...]
    llm_call_count: int
    input_tokens: int
    output_tokens: int
    changes: tuple["CacheEntryChange", ...] = ()


@dataclass(frozen=True)
class ScoreUpdateReport:
    entries: tuple[CacheEntry, ...]
    updated_entry_ids: tuple[str, ...]
    skipped_entry_ids: tuple[str, ...]
    changes: tuple["CacheEntryChange", ...] = ()


@dataclass(frozen=True)
class CacheEntryChange:
    """一条缓存条目的审计变动。"""

    event: str
    entry_id: str
    before: CacheEntry | None
    after: CacheEntry | None
    related_record_ids: tuple[str, ...] = ()
    llm_call_count: int = 0
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, str) or not self.event.strip():
            raise ValueError("event 必须是非空字符串")
        if not isinstance(self.entry_id, str) or not self.entry_id:
            raise ValueError("entry_id 必须是非空字符串")
        if self.before is not None and not isinstance(self.before, CacheEntry):
            raise ValueError("before 必须是 CacheEntry 或 None")
        if self.after is not None and not isinstance(self.after, CacheEntry):
            raise ValueError("after 必须是 CacheEntry 或 None")
        if not isinstance(self.related_record_ids, (tuple, list)) or not all(
            isinstance(item, str) for item in self.related_record_ids
        ):
            raise ValueError("related_record_ids 必须是字符串元组")
        object.__setattr__(self, "related_record_ids", tuple(self.related_record_ids))
        if (
            isinstance(self.llm_call_count, bool)
            or not isinstance(self.llm_call_count, int)
            or self.llm_call_count < 0
        ):
            raise ValueError("llm_call_count 必须是非负整数")
        if self.source is not None and (
            not isinstance(self.source, str) or not self.source
        ):
            raise ValueError("source 必须是非空字符串或 None")


@dataclass
class COAPProposal:
    """一次 COAP 规划决策，等待动作执行线路派发。"""

    action_unit: Any
    interrupt: bool = False
    communication: str = ""
    path: str = "standard"
    publish_to_buffer: bool = True
    plan_id: str = field(default_factory=lambda: f"plan-{uuid4().hex[:12]}")
    request_id: str | None = None
    raw_text: str = ""
    reasoning: str = ""
    input_params: dict[str, Any] = field(default_factory=dict)
    input_positional: list[Any] = field(default_factory=list)
    memory_entry_id: str | None = None
    communication_committed: bool = False
    planned_at: float = field(default_factory=_now)
    planning_started_at: float | None = None
    planning_finished_at: float | None = None
    race_id: str | None = None
    stream_completed: bool = False
    execution_finished: bool = False
    discard_stream_tail: bool = False


@dataclass
class COAPRunResult:
    """一次 COAPRunner 调用的终态结果。"""

    outcome: Any | None = None
    final_metrics: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    stop_reason: str | None = None
    runner_error: str | None = None
    worker_errors: list[tuple[str, str]] = field(default_factory=list)
    cleanup_errors: list[tuple[str, str]] = field(default_factory=list)
    planned_count: int = 0
    executed_count: int = 0
    cache_stats: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def exit_code(self) -> int:
        from src.tasks import TaskStatus

        if self.interrupted:
            return 130
        # Cleanup diagnostics do not invalidate an already recorded task outcome.
        if self.runner_error or self.worker_errors:
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
            "executed_count": self.executed_count,
            "cache_stats": dict(self.cache_stats),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }


__all__ = [
    "Action",
    "ActionSpec",
    "AggregatedCandidate",
    "BufferAddResult",
    "CacheBootResult",
    "CacheContentResult",
    "CacheEntry",
    "CacheEntryChange",
    "CacheInitializationBatch",
    "CacheLookupResult",
    "CacheReason",
    "CacheSource",
    "CacheSourceBundle",
    "CacheUpdateReport",
    "COAPProposal",
    "COAPRunResult",
    "CandidateRecord",
    "CandidateStatus",
    "JsonValue",
    "ScoreUpdateReport",
    "TaskContext",
]
