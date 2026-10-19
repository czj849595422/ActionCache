"""COAP 缓存评分更新（同步版，从 ARC ScoreUpdater 移植）。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
import threading
from typing import Sequence

from .config import ScoreConfig, ScoreStrategy
from .ports import CacheRepositoryPort, ClockPort
from .schemas import CacheEntry, CacheEntryChange, ScoreUpdateReport
from .cache import CacheUpdateError, SystemClock


SCORE_PRIOR_MEAN_KEY = "score_prior_mean"


class ScoreUpdater:
    """基于执行反馈的周期评分递推。"""

    def __init__(
        self,
        repository: CacheRepositoryPort,
        config: ScoreConfig,
        *,
        clock: ClockPort | None = None,
        mutation_lock: threading.RLock | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._clock = clock or SystemClock()
        self._lock = mutation_lock or threading.RLock()
        self._feedback_ids: set[str] = set()
        self._pending_feedback_count = 0
        self._episode_hit_count = 0
        self._episode_attempt_count = 0
        self._episode_success_count = 0
        self._last_score_update_monotonic = self._clock.monotonic()

    def _clip(self, score: float) -> float:
        if not self._config.clip_scores:
            if not 0 <= score <= 1:
                raise CacheUpdateError(
                    "未裁剪的评分超出 [0, 1] 范围",
                    operation="score_update",
                )
            return score
        return min(
            self._config.maximum_score,
            max(self._config.minimum_score, score),
        )

    def record_hits(self, entry_ids: Sequence[str]) -> tuple[CacheEntry, ...]:
        """记录缓存候选被检索命中。"""
        requested = tuple(dict.fromkeys(entry_ids))
        with self._lock:
            entries = list(self._repository.list_entries())
            by_id = {entry.entry_id: index for index, entry in enumerate(entries)}
            missing = [entry_id for entry_id in requested if entry_id not in by_id]
            if missing:
                raise CacheUpdateError(
                    "未知缓存条目 ID: " + ", ".join(missing),
                    operation="record_hits",
                )
            now = self._clock.now()
            updated: list[CacheEntry] = []
            for entry_id in requested:
                index = by_id[entry_id]
                entry = entries[index]
                value = replace(
                    deepcopy(entry),
                    hit_count=entry.hit_count + 1,
                    delta_hit_count=entry.delta_hit_count + 1,
                    last_hit_at=now,
                    updated_at=now,
                )
                entries[index] = value
                updated.append(value)
            self._repository.replace_all(entries)
            self._episode_hit_count += len(requested)
            return tuple(deepcopy(updated))

    def record_feedback(
        self,
        entry_id: str,
        *,
        feedback_id: str,
        success: bool,
    ) -> bool:
        """记录一次执行反馈（成功/失败）；重复 feedback_id 忽略。"""
        with self._lock:
            if feedback_id in self._feedback_ids:
                return False
            entries = list(self._repository.list_entries())
            by_id = {entry.entry_id: index for index, entry in enumerate(entries)}
            if entry_id not in by_id:
                raise CacheUpdateError(
                    f"未知缓存条目 ID: {entry_id}",
                    operation="record_feedback",
                )
            index = by_id[entry_id]
            entry = entries[index]
            delta_success = 1 if success else 0
            value = replace(
                deepcopy(entry),
                attempt_count=entry.attempt_count + 1,
                success_count=entry.success_count + delta_success,
                delta_attempt_count=entry.delta_attempt_count + 1,
                delta_success_count=entry.delta_success_count + delta_success,
                updated_at=self._clock.now(),
            )
            entries[index] = value
            self._repository.replace_all(entries)
            self._feedback_ids.add(feedback_id)
            self._pending_feedback_count += 1
            self._episode_attempt_count += 1
            self._episode_success_count += delta_success
            return True

    def episode_statistics(self) -> dict[str, int]:
        """返回当前 runner 生命周期内的缓存命中与反馈增量。"""
        with self._lock:
            return {
                "hit_count": self._episode_hit_count,
                "attempt_count": self._episode_attempt_count,
                "success_count": self._episode_success_count,
            }

    @staticmethod
    def _prior_mean(entry: CacheEntry) -> float:
        for key in (SCORE_PRIOR_MEAN_KEY, "llm_initial_score_applied"):
            value = entry.metadata.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            value = float(value)
            if math.isfinite(value) and 0 <= value <= 1:
                return value
        return entry.score

    def _next_score(self, entry: CacheEntry) -> tuple[float, bool]:
        attempts = entry.delta_attempt_count
        successes = entry.delta_success_count
        strategy = self._config.strategy
        if strategy is ScoreStrategy.INCREMENTAL_FEEDBACK:
            return entry.score, attempts > 0
        if strategy is ScoreStrategy.FREQUENCY_AWARE and attempts == 0:
            if entry.delta_hit_count == 0 and self._config.idle_decay > 0:
                return entry.score * (1 - self._config.idle_decay), True
            return entry.score, entry.delta_hit_count > 0
        if attempts == 0:
            return entry.score, False

        if strategy is ScoreStrategy.BAYESIAN_SUCCESS_RATE:
            prior_weight = self._config.lambda_value
            prior_mean = self._prior_mean(entry)
            return (
                (prior_mean * prior_weight + entry.success_count)
                / (prior_weight + entry.attempt_count),
                True,
            )

        rate = successes / max(attempts, self._config.epsilon)
        if strategy is ScoreStrategy.CONFIDENCE_ADJUSTED:
            alpha = self._config.lambda_value / (
                self._config.lambda_value + entry.attempt_count
            )
            return (1 - alpha) * entry.score + alpha * rate, True
        if strategy is ScoreStrategy.EXPONENTIAL_SMOOTHING:
            return (
                (1 - self._config.alpha) * entry.score
                + self._config.alpha * rate,
                True,
            )
        if strategy is ScoreStrategy.BOUNDED_ADDITIVE:
            failures = attempts - successes
            return (
                entry.score
                + self._config.success_reward * successes
                - self._config.failure_penalty * failures,
                True,
            )
        return (
            entry.score + self._config.alpha * (rate - entry.score),
            True,
        )

    def should_update(self) -> bool:
        if self._pending_feedback_count > 0:
            return True
        return any(
            entry.delta_attempt_count > 0
            for entry in self._repository.list_entries()
        )
        # 评分递推不调用 LLM，存在任一待处理反馈即更新；
        # 实际消费粒度取决于缓存维护 worker 的轮询周期（约 1s）。
        return self._pending_feedback_count > 0

    def update_scores(
        self,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        match_owner: bool = False,
    ) -> ScoreUpdateReport:
        with self._lock:
            all_entries = list(self._repository.list_entries())
            updated_ids: list[str] = []
            skipped_ids: list[str] = []
            entry_changes: list[CacheEntryChange] = []
            now = self._clock.now()
            for index, entry in enumerate(all_entries):
                if task_scope is not None and entry.task_scope != task_scope:
                    continue
                if match_owner and entry.owner_agent_id != owner_agent_id:
                    continue
                score, changed = self._next_score(entry)
                if not changed:
                    skipped_ids.append(entry.entry_id)
                    continue
                metadata = deepcopy(entry.metadata)
                metadata.setdefault(SCORE_PRIOR_MEAN_KEY, self._prior_mean(entry))
                changes: dict[str, object] = {
                    "score": self._clip(score),
                    "updated_at": now,
                    "metadata": metadata,
                }
                if self._config.reset_interval_counts:
                    changes.update(
                        {
                            "delta_attempt_count": 0,
                            "delta_success_count": 0,
                            "delta_hit_count": 0,
                        }
                    )
                before = deepcopy(entry)
                updated = replace(before, **changes)
                all_entries[index] = updated
                updated_ids.append(entry.entry_id)
                entry_changes.append(
                    CacheEntryChange(
                        event="score_update",
                        entry_id=entry.entry_id,
                        before=before,
                        after=updated,
                        source="score_update",
                    )
                )
            if updated_ids:
                self._repository.replace_all(all_entries)
            self._pending_feedback_count = 0
            self._last_score_update_monotonic = self._clock.monotonic()
            scoped = tuple(
                entry
                for entry in all_entries
                if task_scope is None or entry.task_scope == task_scope
                if not match_owner or entry.owner_agent_id == owner_agent_id
            )
            return ScoreUpdateReport(
                entries=tuple(deepcopy(scoped)),
                updated_entry_ids=tuple(updated_ids),
                skipped_entry_ids=tuple(skipped_ids),
                changes=tuple(deepcopy(entry_changes)),
            )


__all__ = ["ScoreUpdater"]
