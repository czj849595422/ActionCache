"""COAP 缓存模块的结构化端口（同步版本）。"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .schemas import (
    Action,
    ActionSpec,
    CacheContentResult,
    CacheEntry,
    CacheInitializationBatch,
    CacheReason,
    CacheSourceBundle,
    TaskContext,
)


class PortError(RuntimeError):
    def __init__(self, message: str, *, operation: str | None = None):
        super().__init__(message)
        self.operation = operation


class RepositoryPortError(PortError):
    pass


@runtime_checkable
class CacheRepositoryPort(Protocol):
    """缓存条目的同步存储接口。"""

    def list_entries(
        self,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        match_owner: bool = False,
    ) -> tuple[CacheEntry, ...]:
        ...

    def get(self, entry_id: str) -> CacheEntry | None:
        ...

    def find_by_previous_key(
        self,
        canonical_key: str,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        match_owner: bool = False,
    ) -> tuple[CacheEntry, ...]:
        ...

    def upsert(self, entry: CacheEntry) -> None:
        ...

    def delete(self, entry_id: str) -> bool:
        ...

    def replace_all(self, entries: Sequence[CacheEntry]) -> None:
        ...


@runtime_checkable
class CacheSourceLoaderPort(Protocol):
    """从文件加载旧缓存源（后续 JSON 持久化时实现）。"""

    def load(self, source_path: str) -> CacheSourceBundle:
        ...


@runtime_checkable
class CacheInitializerPort(Protocol):
    """LLM 缓存初始化端口。"""

    def initialize(
        self,
        task_context: TaskContext,
        action_space: Sequence[ActionSpec],
        *,
        maximum_entries: int,
        remaining_token_budget: int,
    ) -> CacheInitializationBatch:
        ...


@runtime_checkable
class CacheContentGeneratorPort(Protocol):
    """LLM 缓存内容生成端口。"""

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
        ...


@runtime_checkable
class ActionCanonicalizerPort(Protocol):
    """动作规范化端口。"""

    def canonical_key(self, action: Action) -> str:
        ...

    def canonicalize(self, action: Action) -> Action:
        ...

    def equivalent(self, left: Action, right: Action) -> bool:
        ...


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> float:
        ...

    def monotonic(self) -> float:
        ...


__all__ = [
    "ActionCanonicalizerPort",
    "CacheContentGeneratorPort",
    "CacheInitializerPort",
    "CacheRepositoryPort",
    "CacheSourceLoaderPort",
    "ClockPort",
    "PortError",
    "RepositoryPortError",
]
