"""COAP 缓存生命周期服务（同步、线程安全版本）。

本模块只拥有缓存生命周期算法，不校验动作合法性、不执行动作、不直接调用
具体模型 SDK，也不打开持久化文件。模型回填的缓存内容与仓库通过端口注入。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
import random
import re
import threading
import time
from typing import Collection, Sequence
from uuid import uuid4

from .config import (
    BufferOverflowPolicy,
    BufferUpdateConfig,
    CacheActionCandidatePolicy,
    CacheBootConfig,
    CacheBootMode,
    CacheParameterMode,
    CacheRepresentationConfig,
    CacheSharingMode,
    ReasonSamplingPolicy,
    RetrievalConfig,
    ScoreConfig,
    TieBreakPolicy,
)
from .ports import (
    ActionCanonicalizerPort,
    CacheContentGeneratorPort,
    CacheInitializerPort,
    CacheRepositoryPort,
    ClockPort,
)
from .schemas import (
    Action,
    ActionSpec,
    AggregatedCandidate,
    BufferAddResult,
    CacheBootResult,
    CacheContentResult,
    CacheEntry,
    CacheEntryChange,
    CacheInitializationBatch,
    CacheLookupResult,
    CacheReason,
    CacheSource,
    CacheUpdateReport,
    CandidateRecord,
    CandidateStatus,
    TaskContext,
)


SCORE_PRIOR_MEAN_KEY = "score_prior_mean"


class CacheError(RuntimeError):
    """缓存生命周期操作的基类错误。"""

    def __init__(self, message: str, *, operation: str | None = None):
        super().__init__(message)
        self.operation = operation


class CacheBootError(CacheError):
    pass


class CacheRetrievalError(CacheError):
    pass


class CacheBufferError(CacheError):
    pass


class CacheConflictError(CacheError):
    pass


class CacheUpdateError(CacheError):
    pass


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class DefaultActionCanonicalizer:
    """稳定 JSON 规范化，适用于通用测试环境。"""

    def __init__(
        self,
        *,
        include_agent_id: bool = False,
        case_sensitive_names: bool = True,
    ) -> None:
        self.include_agent_id = include_agent_id
        self.case_sensitive_names = case_sensitive_names

    def _name(self, name: str) -> str:
        return name if self.case_sensitive_names else name.casefold()

    def canonical_key(self, action: Action) -> str:
        if not isinstance(action, Action):
            raise CacheRetrievalError(
                "action 必须是 Action", operation="canonical_key"
            )
        payload: dict[str, object] = {
            "name": self._name(action.name),
            "parameters": action.parameters,
        }
        if self.include_agent_id:
            payload["agent_id"] = action.agent_id
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise CacheRetrievalError(
                f"动作无法规范化: {exc}", operation="canonical_key"
            ) from exc

    def canonicalize(self, action: Action) -> Action:
        result = deepcopy(action)
        result.canonical_key = self.canonical_key(result)
        return result

    def equivalent(self, left: Action, right: Action) -> bool:
        return self.canonical_key(left) == self.canonical_key(right)


class CacheActionCanonicalizer:
    """应用配置的缓存专用动作表示。"""

    def __init__(
        self,
        canonicalizer: ActionCanonicalizerPort,
        parameter_mode: CacheParameterMode = CacheParameterMode.STRIP,
    ) -> None:
        self._canonicalizer = canonicalizer
        self.parameter_mode = CacheParameterMode(parameter_mode)

    def _represent(self, action: Action) -> Action:
        represented = self._canonicalizer.canonicalize(action)
        if self.parameter_mode is CacheParameterMode.STRIP:
            represented.parameters = {}
        represented.canonical_key = None
        return represented

    def canonical_key(self, action: Action) -> str:
        return self._canonicalizer.canonical_key(self._represent(action))

    def canonicalize(self, action: Action) -> Action:
        represented = self._represent(action)
        represented.canonical_key = self._canonicalizer.canonical_key(represented)
        return represented

    def equivalent(self, left: Action, right: Action) -> bool:
        return self.canonical_key(left) == self.canonical_key(right)


def _pair_key(
    previous_action: Action,
    next_action: Action,
    canonicalizer: ActionCanonicalizerPort,
) -> tuple[str, str]:
    return (
        canonicalizer.canonical_key(previous_action),
        canonicalizer.canonical_key(next_action),
    )


def _canonical_entry(
    entry: CacheEntry,
    canonicalizer: ActionCanonicalizerPort,
    **changes: object,
) -> CacheEntry:
    value = deepcopy(entry)
    value.previous_action = canonicalizer.canonicalize(value.previous_action)
    value.next_action = canonicalizer.canonicalize(value.next_action)
    return replace(value, **changes)


def _scope_entries(
    entries: Sequence[CacheEntry],
    task_scope: str | None,
    *,
    owner_agent_id: str | None = None,
    match_owner: bool = False,
) -> tuple[CacheEntry, ...]:
    return tuple(
        entry
        for entry in entries
        if task_scope is None or entry.task_scope == task_scope
        if not match_owner or entry.owner_agent_id == owner_agent_id
    )


def _merge_scope(
    all_entries: Sequence[CacheEntry],
    scoped_entries: Sequence[CacheEntry],
    task_scope: str | None,
    *,
    owner_agent_id: str | None = None,
    match_owner: bool = False,
) -> tuple[CacheEntry, ...]:
    if task_scope is None and not match_owner:
        merged = tuple(scoped_entries)
    else:
        merged = tuple(
            entry
            for entry in all_entries
            if not (
                (task_scope is None or entry.task_scope == task_scope)
                and (not match_owner or entry.owner_agent_id == owner_agent_id)
            )
        ) + tuple(scoped_entries)
    return tuple(sorted(merged, key=lambda entry: entry.entry_id))


def _deduplicate_entries(
    entries: Sequence[CacheEntry],
    canonicalizer: ActionCanonicalizerPort,
    *,
    environment_source_preferred: bool,
) -> tuple[tuple[CacheEntry, ...], tuple[str, ...]]:
    grouped: dict[tuple[str | None, str | None, str, str], list[CacheEntry]] = {}
    for entry in entries:
        canonical = _canonical_entry(entry, canonicalizer)
        previous_key, next_key = _pair_key(
            canonical.previous_action, canonical.next_action, canonicalizer
        )
        grouped.setdefault(
            (
                canonical.task_scope,
                canonical.owner_agent_id,
                previous_key,
                next_key,
            ),
            [],
        ).append(canonical)

    retained: list[CacheEntry] = []
    removed: list[str] = []
    for values in grouped.values():
        ranked = sorted(
            values,
            key=lambda entry: (
                -(
                    1
                    if environment_source_preferred
                    and entry.source is CacheSource.ENV
                    else 0
                ),
                -entry.score,
                -entry.attempt_count,
                -entry.hit_count,
                -entry.updated_at,
                entry.entry_id,
            ),
        )
        retained.append(ranked[0])
        removed.extend(entry.entry_id for entry in ranked[1:])
    return (
        tuple(sorted(retained, key=lambda entry: entry.entry_id)),
        tuple(sorted(removed)),
    )


def _new_entry_score(
    model_score: float,
    scoring: ScoreConfig,
    retrieval: RetrievalConfig,
    *,
    maximum: float | None = None,
) -> float:
    if isinstance(model_score, bool) or not isinstance(model_score, (int, float)):
        raise CacheUpdateError(
            "缓存评分必须是数字" if maximum is None else "LLM 初始评分必须是数字",
            operation="score" if maximum is None else "initial_score",
        )
    score = float(model_score)
    upper = 1.0 if maximum is None else min(float(maximum), 1.0)
    if not math.isfinite(score) or not 0 < score <= upper:
        message = (
            "LLM 初始评分必须在 (0, 0.5] 内"
            if upper <= 0.5
            else "缓存评分必须在 (0, 1] 内"
        )
        raise CacheUpdateError(
            message, operation="score" if maximum is None else "initial_score"
        )
    floor = max(
        scoring.minimum_llm_initial_score,
        retrieval.minimum_score,
    )
    cap = scoring.maximum_score if maximum is None else min(scoring.maximum_score, maximum)
    return min(cap, max(score, floor))


class CacheBootService:
    """缓存初始化：empty / llm_init / reuse_old 三种模式。"""

    def __init__(
        self,
        repository: CacheRepositoryPort,
        config: CacheBootConfig,
        score_config: ScoreConfig,
        retrieval_config: RetrievalConfig,
        *,
        canonicalizer: ActionCanonicalizerPort | None = None,
        clock: ClockPort | None = None,
        initializer: CacheInitializerPort | None = None,
        mutation_lock: threading.RLock | None = None,
        environment_source_preferred: bool = True,
    ) -> None:
        self._repository = repository
        self._config = config
        self._scoring = score_config
        self._retrieval = retrieval_config
        self._canonicalizer = canonicalizer or DefaultActionCanonicalizer()
        self._clock = clock or SystemClock()
        self._initializer = initializer
        self._lock = mutation_lock or threading.RLock()
        self._environment_source_preferred = environment_source_preferred

    def _known_action(self, action: Action, action_names: set[str]) -> bool:
        if action.name == self._retrieval.start_action_name:
            return self._retrieval.allow_start_sentinel
        return action.name in action_names

    def _prepare_entry(
        self,
        entry: CacheEntry,
        *,
        task_scope: str | None,
        owner_agent_id: str | None,
        force_llm_source: bool,
        apply_score_floor: bool,
    ) -> CacheEntry:
        raw_score = entry.score
        score = (
            _new_entry_score(
                raw_score,
                self._scoring,
                self._retrieval,
                maximum=(
                    self._scoring.maximum_llm_initial_score
                    if force_llm_source
                    else None
                ),
            )
            if apply_score_floor
            else raw_score
        )
        metadata = deepcopy(entry.metadata)
        if force_llm_source:
            metadata.update(
                {
                    "llm_initial_score_raw": raw_score,
                    "llm_initial_score_applied": score,
                    "llm_initial_score_required": True,
                }
            )
        metadata.setdefault(SCORE_PRIOR_MEAN_KEY, score)
        now = self._clock.now()
        created_at = now if force_llm_source else entry.created_at
        return _canonical_entry(
            entry,
            self._canonicalizer,
            score=score,
            source=CacheSource.LLM if force_llm_source else entry.source,
            hit_count=0 if force_llm_source else entry.hit_count,
            attempt_count=0 if force_llm_source else entry.attempt_count,
            success_count=0 if force_llm_source else entry.success_count,
            delta_hit_count=0,
            delta_attempt_count=0,
            delta_success_count=0,
            created_at=created_at,
            updated_at=max(now, created_at),
            last_hit_at=None if force_llm_source else entry.last_hit_at,
            task_scope=task_scope,
            owner_agent_id=owner_agent_id,
            metadata=metadata,
        )

    def boot(
        self,
        task_context: TaskContext,
        action_space: Sequence[ActionSpec],
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        old_entries: Sequence[CacheEntry] | None = None,
    ) -> CacheBootResult:
        if not isinstance(task_context, TaskContext):
            raise CacheBootError(
                "task_context 必须是 TaskContext", operation="boot"
            )
        if isinstance(action_space, (str, bytes)) or not isinstance(
            action_space, (tuple, list)
        ):
            raise CacheBootError(
                "action_space 必须是序列", operation="boot"
            )
        action_names: set[str] = set()
        for action_spec in action_space:
            if not isinstance(action_spec, ActionSpec):
                raise CacheBootError(
                    "action_space 必须包含 ActionSpec", operation="boot"
                )
            action_names.add(action_spec.name)

        with self._lock:
            start_action_name = self._retrieval.start_action_name
            all_entries = tuple(
                entry
                for entry in self._repository.list_entries()
                if not (
                    entry.previous_action.name == start_action_name
                    and (task_scope is None or entry.task_scope == task_scope)
                    and entry.owner_agent_id == owner_agent_id
                )
            )
            requested_mode = self._config.resolved_mode
            mode = requested_mode
            if requested_mode is CacheBootMode.REUSE_PREVIOUS:
                mode = (
                    CacheBootMode.REUSE_OLD
                    if old_entries is not None
                    else CacheBootMode.LLM_INIT
                )
            candidates: list[CacheEntry] = []
            filtered_count = 0
            llm_calls = 0
            input_tokens = 0
            output_tokens = 0
            duplicate_count = 0

            if mode is CacheBootMode.LLM_INIT:
                if self._initializer is None:
                    raise CacheBootError(
                        "llm_init 模式需要 CacheInitializerPort",
                        operation="boot",
                    )
                batch = self._initializer.initialize(
                    task_context,
                    action_space,
                    maximum_entries=self._config.target_entry_count,
                    remaining_token_budget=self._config.initialization_token_budget,
                )
                if not isinstance(batch, CacheInitializationBatch):
                    raise CacheBootError(
                        "initializer 必须返回 CacheInitializationBatch",
                        operation="boot",
                    )
                llm_calls = 1
                input_tokens = batch.input_tokens
                output_tokens = batch.output_tokens
                dropped_by_parser = batch.metadata.get("dropped_entry_count", 0)
                if (
                    isinstance(dropped_by_parser, int)
                    and not isinstance(dropped_by_parser, bool)
                    and dropped_by_parser >= 0
                ):
                    filtered_count += dropped_by_parser
                for entry in batch.entries:
                    if (
                        entry.previous_action.name == start_action_name
                        or entry.next_action.name == start_action_name
                    ):
                        filtered_count += 1
                        continue
                    if self._config.filter_unknown_actions and (
                        not self._known_action(entry.previous_action, action_names)
                        or not self._known_action(entry.next_action, action_names)
                    ):
                        filtered_count += 1
                        continue
                    candidates.append(
                        self._prepare_entry(
                            entry,
                            task_scope=task_scope,
                            owner_agent_id=owner_agent_id,
                            force_llm_source=True,
                            apply_score_floor=True,
                        )
                    )
            elif mode is CacheBootMode.REUSE_OLD:
                if old_entries is None:
                    raise CacheBootError(
                        "reuse_previous/reuse_old 模式需要注入 old_entries",
                        operation="boot",
                    )
                for entry in old_entries:
                    if not isinstance(entry, CacheEntry):
                        raise CacheBootError(
                            "old_entries 必须包含 CacheEntry", operation="boot"
                        )
                    if (
                        entry.previous_action.name == start_action_name
                        or entry.next_action.name == start_action_name
                    ):
                        filtered_count += 1
                        continue
                    if self._config.filter_unknown_actions and (
                        not self._known_action(entry.previous_action, action_names)
                        or not self._known_action(entry.next_action, action_names)
                    ):
                        filtered_count += 1
                        continue
                    candidates.append(
                        self._prepare_entry(
                            entry,
                            task_scope=task_scope,
                            owner_agent_id=owner_agent_id,
                            force_llm_source=False,
                            apply_score_floor=self._config.normalize_loaded_scores,
                        )
                    )

            deduplicated, removed_ids = _deduplicate_entries(
                candidates,
                self._canonicalizer,
                environment_source_preferred=self._environment_source_preferred,
            )
            duplicate_count += len(removed_ids)
            removed_by_id = {entry.entry_id: entry for entry in candidates}
            merged = _merge_scope(
                all_entries,
                deduplicated,
                task_scope,
                owner_agent_id=owner_agent_id,
                match_owner=True,
            )
            self._repository.replace_all(merged)
            changes: list[CacheEntryChange] = []
            if mode is CacheBootMode.LLM_INIT:
                changes.extend(
                    CacheEntryChange(
                        event="boot_create",
                        entry_id=entry.entry_id,
                        before=None,
                        after=entry,
                        llm_call_count=llm_calls,
                        source=entry.source.value,
                    )
                    for entry in deduplicated
                )
            elif mode is CacheBootMode.REUSE_OLD:
                changes.extend(
                    CacheEntryChange(
                        event="boot_reuse",
                        entry_id=entry.entry_id,
                        before=None,
                        after=entry,
                        source=(
                            CacheBootMode.REUSE_PREVIOUS.value
                            if requested_mode is CacheBootMode.REUSE_PREVIOUS
                            else CacheBootMode.REUSE_OLD.value
                        ),
                    )
                    for entry in deduplicated
                )
            changes.extend(
                CacheEntryChange(
                    event="deduplicate_delete",
                    entry_id=entry_id,
                    before=removed_by_id.get(entry_id),
                    after=None,
                    source="boot",
                )
                for entry_id in removed_ids
            )
            return CacheBootResult(
                mode=(
                    CacheBootMode.REUSE_PREVIOUS.value
                    if requested_mode is CacheBootMode.REUSE_PREVIOUS
                    and mode is CacheBootMode.REUSE_OLD
                    else mode.value
                ),
                entries=tuple(deepcopy(deduplicated)),
                created_count=(
                    len(deduplicated) if mode is CacheBootMode.LLM_INIT else 0
                ),
                reused_count=(
                    len(deduplicated) if mode is CacheBootMode.REUSE_OLD else 0
                ),
                filtered_count=filtered_count,
                duplicate_count=duplicate_count,
                llm_call_count=llm_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                changes=tuple(deepcopy(changes)),
            )


class CacheRetriever:
    """按上一动作检索 top-k 缓存。"""

    def __init__(
        self,
        repository: CacheRepositoryPort,
        config: RetrievalConfig,
        *,
        canonicalizer: ActionCanonicalizerPort | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._canonicalizer = canonicalizer or DefaultActionCanonicalizer()

    def _sort_key(self, entry: CacheEntry) -> tuple[object, ...]:
        if self._config.tie_break_policy is TieBreakPolicy.SCORE_THEN_RECENCY:
            return (-entry.score, -entry.updated_at, entry.entry_id)
        if self._config.tie_break_policy is TieBreakPolicy.SCORE_THEN_SUCCESS_RATE:
            success_rate = -1.0 if entry.success_rate is None else entry.success_rate
            return (-entry.score, -success_rate, entry.entry_id)
        return (-entry.score, entry.entry_id)

    def retrieve(
        self,
        previous_action: Action | None,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
    ) -> CacheLookupResult:
        if previous_action is None:
            if not self._config.allow_start_sentinel:
                raise CacheRetrievalError(
                    "禁用 start sentinel 时 previous_action 必填",
                    operation="retrieve",
                )
            previous_action = Action(name=self._config.start_action_name)
        if not isinstance(previous_action, Action):
            raise CacheRetrievalError(
                "previous_action 必须是 Action 或 None", operation="retrieve"
            )
        previous_key = self._canonicalizer.canonical_key(previous_action)
        if self._config.require_exact_previous_action:
            matched = self._repository.find_by_previous_key(
                previous_key,
                task_scope=task_scope,
                owner_agent_id=owner_agent_id,
                match_owner=True,
            )
        else:
            entries = self._repository.list_entries(
                task_scope=task_scope,
                owner_agent_id=owner_agent_id,
                match_owner=True,
            )
            matched = tuple(
                entry
                for entry in entries
                if entry.previous_action.name == previous_action.name
            )
        eligible = tuple(
            entry
            for entry in matched
            if entry.score >= self._config.minimum_score
        )
        ranked = tuple(sorted(eligible, key=self._sort_key))
        return CacheLookupResult(
            previous_key=previous_key,
            entries=tuple(deepcopy(ranked[: self._config.top_k])),
            matched_count=len(matched),
            filtered_count=len(matched) - len(eligible),
        )


class CandidateBuffer:
    """普通动作规划暂存缓冲区（线程安全）。"""

    def __init__(
        self,
        config: BufferUpdateConfig,
        *,
        clock: ClockPort | None = None,
        initial_monotonic: float | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or SystemClock()
        self._records: dict[str, CandidateRecord] = {}
        self._decision_index: dict[tuple[str, str | None], str] = {}
        self._last_flush_monotonic = (
            self._clock.monotonic()
            if initial_monotonic is None
            else float(initial_monotonic)
        )
        if self._last_flush_monotonic < 0:
            raise CacheBufferError(
                "initial_monotonic 必须非负", operation="buffer_init"
            )
        self._lock = threading.RLock()

    @staticmethod
    def _same_decision(left: CandidateRecord, right: CandidateRecord) -> bool:
        return (
            left.agent_id == right.agent_id
            and left.previous_action == right.previous_action
            and left.next_action == right.next_action
            and left.reasoning == right.reasoning
            and left.status is right.status
            and left.cache_statistics_applied == right.cache_statistics_applied
        )

    @staticmethod
    def _decision_key(record: CandidateRecord) -> tuple[str, str | None]:
        return (record.decision_id, record.plan_id)

    def _should_flush_unlocked(self, now_monotonic: float) -> bool:
        return bool(self._records) and (
            len(self._records) >= self._config.buffer_size_threshold
            or now_monotonic - self._last_flush_monotonic
            >= self._config.update_interval_seconds
        )

    def add(self, record: CandidateRecord) -> BufferAddResult:
        if not isinstance(record, CandidateRecord):
            raise CacheBufferError(
                "record 必须是 CandidateRecord", operation="buffer_add"
            )
        with self._lock:
            now_monotonic = self._clock.monotonic()
            if self._config.deduplicate_by_decision_id:
                decision_key = self._decision_key(record)
                existing_id = self._decision_index.get(decision_key)
                if existing_id is not None:
                    existing = self._records[existing_id]
                    if not self._same_decision(existing, record):
                        raise CacheConflictError(
                            f"decision_id {record.decision_id} 存在冲突候选",
                            operation="buffer_add",
                        )
                    return BufferAddResult(
                        accepted=False,
                        duplicate=True,
                        size=len(self._records),
                        should_flush=self._should_flush_unlocked(now_monotonic),
                    )
            if record.record_id in self._records:
                if self._records[record.record_id] != record:
                    raise CacheConflictError(
                        f"record_id {record.record_id} 存在冲突候选",
                        operation="buffer_add",
                    )
                return BufferAddResult(
                    accepted=False,
                    duplicate=True,
                    size=len(self._records),
                    should_flush=self._should_flush_unlocked(now_monotonic),
                )

            dropped: tuple[str, ...] = ()
            if len(self._records) >= self._config.maximum_buffer_records:
                if (
                    self._config.buffer_overflow_policy
                    is BufferOverflowPolicy.ERROR
                ):
                    raise CacheBufferError(
                        "候选缓冲区容量已满", operation="buffer_add"
                    )
                oldest_id = next(iter(self._records))
                oldest = self._records.pop(oldest_id)
                self._decision_index.pop(self._decision_key(oldest), None)
                dropped = (oldest_id,)

            copied = deepcopy(record)
            self._records[copied.record_id] = copied
            if self._config.deduplicate_by_decision_id:
                self._decision_index[self._decision_key(copied)] = copied.record_id
            return BufferAddResult(
                accepted=True,
                duplicate=False,
                size=len(self._records),
                should_flush=self._should_flush_unlocked(now_monotonic),
                dropped_record_ids=dropped,
            )

    def extend(
        self, records: Sequence[CandidateRecord]
    ) -> tuple[BufferAddResult, ...]:
        results: list[BufferAddResult] = []
        for record in records:
            results.append(self.add(record))
        return tuple(results)

    def snapshot(self) -> tuple[CandidateRecord, ...]:
        with self._lock:
            return tuple(deepcopy(tuple(self._records.values())))

    def acknowledge(self, record_ids: Collection[str]) -> None:
        with self._lock:
            for record_id in record_ids:
                record = self._records.pop(record_id, None)
                if record is not None:
                    self._decision_index.pop(self._decision_key(record), None)
            self._last_flush_monotonic = self._clock.monotonic()

    def should_flush(self) -> bool:
        with self._lock:
            return self._should_flush_unlocked(self._clock.monotonic())

    def size(self) -> int:
        with self._lock:
            return len(self._records)


class CandidateAggregator:
    """按 (previous_action, next_action) 聚合候选记录。"""

    def __init__(
        self,
        config: BufferUpdateConfig,
        *,
        canonicalizer: ActionCanonicalizerPort | None = None,
        random_seed: int | None = 0,
    ) -> None:
        self._config = config
        self._canonicalizer = canonicalizer or DefaultActionCanonicalizer()
        self._random_seed = random_seed

    @staticmethod
    def _unique_reasons(records: Sequence[CandidateRecord]) -> list[str]:
        seen: set[str] = set()
        reasons: list[str] = []
        for record in records:
            reason = record.reasoning.strip()
            if reason and reason not in seen:
                seen.add(reason)
                reasons.append(reason)
        return reasons

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"\w+", text.casefold()))

    def _sample_reasons(
        self, reasons: list[str], pair_key: tuple[str, str]
    ) -> tuple[str, ...]:
        limit = self._config.maximum_reasons_per_pair
        if len(reasons) <= limit:
            return tuple(reasons)
        policy = self._config.reason_sampling_policy
        if policy is ReasonSamplingPolicy.LATEST:
            return tuple(reasons[-limit:])
        if policy is ReasonSamplingPolicy.RANDOM:
            seed_text = f"{self._random_seed}:{pair_key[0]}:{pair_key[1]}"
            rng = random.Random(seed_text)
            selected = sorted(rng.sample(range(len(reasons)), limit))
            return tuple(reasons[index] for index in selected)
        if policy is ReasonSamplingPolicy.DIVERSE:
            selected = [0]
            token_sets = [self._tokens(reason) for reason in reasons]
            while len(selected) < limit:
                candidates = [
                    index for index in range(len(reasons)) if index not in selected
                ]

                def novelty(index: int) -> tuple[float, int]:
                    closest = 1.0
                    for chosen in selected:
                        union = token_sets[index] | token_sets[chosen]
                        similarity = (
                            len(token_sets[index] & token_sets[chosen]) / len(union)
                            if union
                            else 1.0
                        )
                        closest = min(closest, 1.0 - similarity)
                    return closest, -index

                selected.append(max(candidates, key=novelty))
            return tuple(reasons[index] for index in sorted(selected))
        return tuple(reasons[:limit])

    def aggregate(
        self, records: Sequence[CandidateRecord]
    ) -> tuple[AggregatedCandidate, ...]:
        if isinstance(records, (str, bytes)) or not isinstance(
            records, (tuple, list)
        ):
            raise CacheBufferError(
                "records 必须是序列", operation="aggregate"
            )
        grouped: dict[tuple[str, str], list[CandidateRecord]] = {}
        for record in records:
            if not isinstance(record, CandidateRecord):
                raise CacheBufferError(
                    "records 必须包含 CandidateRecord", operation="aggregate"
                )
            grouped.setdefault(
                _pair_key(
                    record.previous_action,
                    record.next_action,
                    self._canonicalizer,
                ),
                [],
            ).append(record)

        results: list[AggregatedCandidate] = []
        for pair, values in sorted(grouped.items()):
            ordered = sorted(values, key=lambda item: (item.created_at, item.record_id))
            success_count = sum(
                item.status is CandidateStatus.SUCCESS for item in ordered
            )
            failure_count = sum(
                item.status is CandidateStatus.FAILURE for item in ordered
            )
            uncertain_count = sum(
                item.status is CandidateStatus.UNCERTAIN for item in ordered
            )
            pending = tuple(
                item
                for item in ordered
                if not item.cache_statistics_applied
                and item.status is not CandidateStatus.UNCERTAIN
            )
            results.append(
                AggregatedCandidate(
                    previous_action=self._canonicalizer.canonicalize(
                        ordered[0].previous_action
                    ),
                    next_action=self._canonicalizer.canonicalize(
                        ordered[0].next_action
                    ),
                    success_count=success_count,
                    failure_count=failure_count,
                    uncertain_count=uncertain_count,
                    reasons=self._sample_reasons(
                        self._unique_reasons(ordered), pair
                    ),
                    record_ids=tuple(item.record_id for item in ordered),
                    first_created_at=ordered[0].created_at,
                    last_created_at=ordered[-1].created_at,
                    pending_attempt_count=len(pending),
                    pending_success_count=sum(
                        item.status is CandidateStatus.SUCCESS for item in pending
                    ),
                    metadata={
                        "decision_ids": [item.decision_id for item in ordered]
                    },
                )
            )
        return tuple(results)


class CacheContentUpdater:
    """聚合候选 -> LLM 抽象 reason -> 创建/更新缓存条目。"""

    def __init__(
        self,
        repository: CacheRepositoryPort,
        config: BufferUpdateConfig,
        score_config: ScoreConfig,
        retrieval_config: RetrievalConfig,
        content_generator: CacheContentGeneratorPort,
        *,
        canonicalizer: ActionCanonicalizerPort | None = None,
        clock: ClockPort | None = None,
        mutation_lock: threading.RLock | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._scoring = score_config
        self._retrieval = retrieval_config
        self._content_generator = content_generator
        self._canonicalizer = canonicalizer or DefaultActionCanonicalizer()
        self._clock = clock or SystemClock()
        self._lock = mutation_lock or threading.RLock()
        self._episode_attempt_count = 0
        self._episode_success_count = 0

    def episode_statistics(self) -> dict[str, int]:
        """返回当前 runner 生命周期内由候选缓冲应用的统计增量。"""
        with self._lock:
            return {
                "hit_count": 0,
                "attempt_count": self._episode_attempt_count,
                "success_count": self._episode_success_count,
            }

    def apply(
        self,
        candidates: Sequence[AggregatedCandidate],
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
    ) -> CacheUpdateReport:
        if isinstance(candidates, (str, bytes)) or not isinstance(
            candidates, (tuple, list)
        ):
            raise CacheUpdateError(
                "candidates 必须是序列", operation="content_update"
            )
        for candidate in candidates:
            if not isinstance(candidate, AggregatedCandidate):
                raise CacheUpdateError(
                    "candidates 必须包含 AggregatedCandidate",
                    operation="content_update",
                )

        with self._lock:
            all_entries = self._repository.list_entries()
            scoped, removed_ids = _deduplicate_entries(
                _scope_entries(
                    all_entries,
                    task_scope,
                    owner_agent_id=owner_agent_id,
                    match_owner=True,
                ),
                self._canonicalizer,
                environment_source_preferred=self._config.environment_source_preferred,
            )
            before_by_id = {entry.entry_id: entry for entry in all_entries}
            by_pair = {
                _pair_key(
                    entry.previous_action,
                    entry.next_action,
                    self._canonicalizer,
                ): entry
                for entry in scoped
            }
            created: list[str] = []
            updated: list[str] = []
            processed_record_ids: list[str] = []
            llm_calls = 0
            input_tokens = 0
            output_tokens = 0
            episode_attempt_count = 0
            episode_success_count = 0
            changes: list[CacheEntryChange] = [
                CacheEntryChange(
                    event="deduplicate_delete",
                    entry_id=entry_id,
                    before=before_by_id.get(entry_id),
                    after=None,
                    source="content_update",
                )
                for entry_id in removed_ids
            ]

            for candidate in candidates:
                previous_action = self._canonicalizer.canonicalize(
                    candidate.previous_action
                )
                next_action = self._canonicalizer.canonicalize(
                    candidate.next_action
                )
                if previous_action.name == self._retrieval.start_action_name:
                    processed_record_ids.extend(candidate.record_ids)
                    continue
                pair = _pair_key(
                    previous_action, next_action, self._canonicalizer
                )
                existing = by_pair.get(pair)
                generated = self._content_generator.generate(
                    previous_action,
                    next_action,
                    candidate.reasons,
                    existing_reason=None if existing is None else existing.reason,
                    require_initial_score=existing is None,
                    owner_agent_id=owner_agent_id,
                )
                if not isinstance(generated, CacheContentResult):
                    raise CacheUpdateError(
                        "content generator 必须返回 CacheContentResult",
                        operation="content_update",
                    )
                llm_calls += 1
                input_tokens += generated.input_tokens
                output_tokens += generated.output_tokens
                now = self._clock.now()
                has_environment_evidence = (
                    candidate.success_count + candidate.failure_count > 0
                )

                if existing is None:
                    if generated.initial_score is None:
                        raise CacheUpdateError(
                            "每个新缓存对都需要 LLM 初始评分",
                            operation="content_update",
                        )
                    applied_score = _new_entry_score(
                        generated.initial_score,
                        self._scoring,
                        self._retrieval,
                        maximum=self._scoring.maximum_llm_initial_score,
                    )
                    if not has_environment_evidence:
                        applied_score = min(
                            applied_score,
                            self._config.default_uncertain_score,
                        )
                    metadata = {
                        "llm_initial_score_raw": generated.initial_score,
                        "llm_initial_score_applied": applied_score,
                        "llm_initial_score_required": True,
                        SCORE_PRIOR_MEAN_KEY: applied_score,
                    }
                    if generated.model_name is not None:
                        metadata["llm_score_model"] = generated.model_name
                    entry = CacheEntry(
                        previous_action=previous_action,
                        next_action=next_action,
                        reason=deepcopy(generated.reason),
                        score=applied_score,
                        source=(
                            CacheSource.ENV
                            if has_environment_evidence
                            else CacheSource.LLM
                        ),
                        attempt_count=candidate.pending_attempt_count,
                        success_count=candidate.pending_success_count,
                        delta_attempt_count=candidate.pending_attempt_count,
                        delta_success_count=candidate.pending_success_count,
                        created_at=now,
                        updated_at=now,
                        task_scope=task_scope,
                        owner_agent_id=owner_agent_id,
                        metadata=metadata,
                    )
                    by_pair[pair] = entry
                    created.append(entry.entry_id)
                    episode_attempt_count += candidate.pending_attempt_count
                    episode_success_count += candidate.pending_success_count
                    changes.append(
                        CacheEntryChange(
                            event="content_create",
                            entry_id=entry.entry_id,
                            before=None,
                            after=entry,
                            related_record_ids=candidate.record_ids,
                            llm_call_count=1,
                            source=entry.source.value,
                        )
                    )
                else:
                    before = deepcopy(existing)
                    entry = replace(
                        before,
                        reason=deepcopy(generated.reason),
                        source=(
                            CacheSource.ENV
                            if has_environment_evidence
                            else existing.source
                        ),
                        attempt_count=existing.attempt_count
                        + candidate.pending_attempt_count,
                        success_count=existing.success_count
                        + candidate.pending_success_count,
                        delta_attempt_count=existing.delta_attempt_count
                        + candidate.pending_attempt_count,
                        delta_success_count=existing.delta_success_count
                        + candidate.pending_success_count,
                        updated_at=now,
                    )
                    by_pair[pair] = entry
                    updated.append(entry.entry_id)
                    episode_attempt_count += candidate.pending_attempt_count
                    episode_success_count += candidate.pending_success_count
                    changes.append(
                        CacheEntryChange(
                            event="content_update",
                            entry_id=entry.entry_id,
                            before=before,
                            after=entry,
                            related_record_ids=candidate.record_ids,
                            llm_call_count=1,
                            source=entry.source.value,
                        )
                    )
                processed_record_ids.extend(candidate.record_ids)

            scoped_updated = tuple(
                sorted(by_pair.values(), key=lambda entry: entry.entry_id)
            )
            merged = _merge_scope(
                all_entries,
                scoped_updated,
                task_scope,
                owner_agent_id=owner_agent_id,
                match_owner=True,
            )
            self._repository.replace_all(merged)
            self._episode_attempt_count += episode_attempt_count
            self._episode_success_count += episode_success_count
            return CacheUpdateReport(
                entries=tuple(deepcopy(scoped_updated)),
                created_entry_ids=tuple(created),
                updated_entry_ids=tuple(updated),
                removed_duplicate_ids=removed_ids,
                processed_record_ids=tuple(processed_record_ids),
                llm_call_count=llm_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                changes=tuple(deepcopy(changes)),
            )


class CacheService:
    """组合门面：共享一把变更锁的缓存服务集合（同步版）。"""

    def __init__(
        self,
        repository: CacheRepositoryPort,
        *,
        boot_config: CacheBootConfig | None = None,
        representation_config: CacheRepresentationConfig | None = None,
        retrieval_config: RetrievalConfig | None = None,
        buffer_config: BufferUpdateConfig | None = None,
        score_config: ScoreConfig | None = None,
        sharing_mode: CacheSharingMode = CacheSharingMode.SHARED,
        canonicalizer: ActionCanonicalizerPort | None = None,
        clock: ClockPort | None = None,
        initializer: CacheInitializerPort | None = None,
        content_generator: CacheContentGeneratorPort | None = None,
    ) -> None:
        action_canonicalizer = canonicalizer or DefaultActionCanonicalizer()
        representation = representation_config or CacheRepresentationConfig()
        cache_canonicalizer = CacheActionCanonicalizer(
            action_canonicalizer,
            representation.parameter_mode,
        )
        clock = clock or SystemClock()
        mutation_lock = threading.RLock()
        self.repository = repository
        self.clock = clock
        self.action_canonicalizer = action_canonicalizer
        self.cache_canonicalizer = cache_canonicalizer
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self.sharing_mode = CacheSharingMode(sharing_mode)
        self.buffer = CandidateBuffer(buffer_config or BufferUpdateConfig(), clock=clock)
        self.aggregator = CandidateAggregator(
            buffer_config or BufferUpdateConfig(),
            canonicalizer=cache_canonicalizer,
            random_seed=0,
        )
        self.boot = CacheBootService(
            repository,
            boot_config or CacheBootConfig(),
            score_config or ScoreConfig(),
            self.retrieval_config,
            canonicalizer=cache_canonicalizer,
            clock=clock,
            initializer=initializer,
            mutation_lock=mutation_lock,
            environment_source_preferred=(
                (buffer_config or BufferUpdateConfig()).environment_source_preferred
            ),
        )
        self.retriever = CacheRetriever(
            repository,
            self.retrieval_config,
            canonicalizer=cache_canonicalizer,
        )
        self.content_updater = (
            None
            if content_generator is None
            else CacheContentUpdater(
                repository,
                buffer_config or BufferUpdateConfig(),
                score_config or ScoreConfig(),
                self.retrieval_config,
                content_generator,
                canonicalizer=cache_canonicalizer,
                clock=clock,
                mutation_lock=mutation_lock,
            )
        )
        self._episode_attempt_count = 0
        self._episode_success_count = 0

    def episode_statistics(self) -> dict[str, int]:
        """返回当前 runner 生命周期内由已有缓存统计的增量。"""
        return {
            "hit_count": 0,
            "attempt_count": self._episode_attempt_count,
            "success_count": self._episode_success_count,
        }

    def cache_key(self, action: Action) -> str:
        return self.cache_canonicalizer.canonical_key(action)

    def matches_cached_action(self, cached: Action, concrete: Action) -> bool:
        return self.cache_canonicalizer.equivalent(cached, concrete)

    def owner_for(self, agent_id: str) -> str | None:
        return (
            agent_id
            if self.sharing_mode is CacheSharingMode.PER_AGENT
            else None
        )

    def retrieve(
        self,
        previous_action: Action | None,
        *,
        task_scope: str,
        agent_id: str,
    ) -> CacheLookupResult:
        return self.retriever.retrieve(
            previous_action,
            task_scope=task_scope,
            owner_agent_id=self.owner_for(agent_id),
        )

    def scoped_entry_count(self, *, task_scope: str, agent_id: str) -> int:
        entries = self.repository.list_entries(
            task_scope=task_scope,
            owner_agent_id=self.owner_for(agent_id),
            match_owner=True,
        )
        return len(entries)

    def partition_candidates(
        self, records: Sequence[CandidateRecord]
    ) -> tuple[tuple[str | None, tuple[CandidateRecord, ...]], ...]:
        if self.sharing_mode is CacheSharingMode.SHARED:
            return ((None, tuple(records)),)
        grouped: dict[str, list[CandidateRecord]] = {}
        for record in records:
            grouped.setdefault(record.agent_id, []).append(record)
        return tuple(
            (agent_id, tuple(grouped[agent_id])) for agent_id in sorted(grouped)
        )

    def maintenance_owners(self, agent_ids: Sequence[str]) -> tuple[str | None, ...]:
        if self.sharing_mode is CacheSharingMode.SHARED:
            return (None,)
        return tuple(sorted(set(agent_ids)))

    def apply_existing_statistics(
        self,
        records: Sequence[CandidateRecord],
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
    ) -> CacheUpdateReport:
        """Apply pending success/failure counts only to existing cache entries."""
        if not records:
            return CacheUpdateReport(
                entries=(),
                created_entry_ids=(),
                updated_entry_ids=(),
                removed_duplicate_ids=(),
                processed_record_ids=(),
                llm_call_count=0,
                input_tokens=0,
                output_tokens=0,
                changes=(),
            )

        aggregated = self.aggregator.aggregate(records)
        all_entries = list(self.repository.list_entries())
        scoped = _scope_entries(
            all_entries,
            task_scope,
            owner_agent_id=owner_agent_id,
            match_owner=True,
        )
        by_pair = {
            _pair_key(
                entry.previous_action,
                entry.next_action,
                self.cache_canonicalizer,
            ): entry
            for entry in scoped
        }
        by_id = {entry.entry_id: index for index, entry in enumerate(all_entries)}
        updated_ids: list[str] = []
        processed_record_ids: list[str] = []
        episode_attempt_count = 0
        episode_success_count = 0
        changes: list[CacheEntryChange] = []
        now = self.clock.now()

        for candidate in aggregated:
            if candidate.pending_attempt_count == 0:
                continue
            previous_action = self.cache_canonicalizer.canonicalize(
                candidate.previous_action
            )
            next_action = self.cache_canonicalizer.canonicalize(candidate.next_action)
            pair = _pair_key(previous_action, next_action, self.cache_canonicalizer)
            existing = by_pair.get(pair)
            if existing is None:
                continue
            index = by_id[existing.entry_id]
            before = deepcopy(existing)
            updated = replace(
                before,
                source=CacheSource.ENV,
                attempt_count=before.attempt_count
                + candidate.pending_attempt_count,
                success_count=before.success_count
                + candidate.pending_success_count,
                delta_attempt_count=before.delta_attempt_count
                + candidate.pending_attempt_count,
                delta_success_count=before.delta_success_count
                + candidate.pending_success_count,
                updated_at=now,
            )
            all_entries[index] = updated
            by_pair[pair] = updated
            updated_ids.append(updated.entry_id)
            processed_record_ids.extend(candidate.record_ids)
            episode_attempt_count += candidate.pending_attempt_count
            episode_success_count += candidate.pending_success_count
            changes.append(
                CacheEntryChange(
                    event="statistics_update",
                    entry_id=updated.entry_id,
                    before=before,
                    after=updated,
                    related_record_ids=candidate.record_ids,
                    source="statistics_update",
                )
            )

        if updated_ids:
            self.repository.replace_all(all_entries)
            self._episode_attempt_count += episode_attempt_count
            self._episode_success_count += episode_success_count

        scoped_after = tuple(
            entry
            for entry in all_entries
            if task_scope is None or entry.task_scope == task_scope
            if entry.owner_agent_id == owner_agent_id
        )
        return CacheUpdateReport(
            entries=tuple(deepcopy(scoped_after)),
            created_entry_ids=(),
            updated_entry_ids=tuple(updated_ids),
            removed_duplicate_ids=(),
            processed_record_ids=tuple(processed_record_ids),
            llm_call_count=0,
            input_tokens=0,
            output_tokens=0,
            changes=tuple(deepcopy(changes)),
        )


__all__ = [
    "BufferAddResult",
    "CacheActionCanonicalizer",
    "CacheBootError",
    "CacheBootResult",
    "CacheBootService",
    "CacheBufferError",
    "CacheConflictError",
    "CacheContentUpdater",
    "CacheError",
    "CacheLookupResult",
    "CacheRetrievalError",
    "CacheRetriever",
    "CacheService",
    "CacheUpdateError",
    "CacheUpdateReport",
    "CandidateAggregator",
    "CandidateBuffer",
    "DefaultActionCanonicalizer",
    "SystemClock",
]
