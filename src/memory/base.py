"""统一记忆接口：基础协议 + 可选能力协议。

约定：所有实现必须线程安全；方法均为同步 def；未实现的能力调用抛
MemoryCapabilityError（可先用 require_capability 预检）。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from .schemas import (
    MemoryCapabilityError,
    MemoryEntry,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStats,
    UNSET,
)

EntryCallback = Callable[[MemoryEntry], None]
SubscriptionToken = str


@runtime_checkable
class MemoryStore(Protocol):
    """记忆存储基础协议（同步、线程安全）。"""

    def get(self, entry_id: str) -> MemoryEntry | None:
        """按 id 取一条记忆；不存在返回 None。"""

    def put(self, entry: MemoryEntry) -> MemoryEntry:
        """按 entry_id upsert：已存在则刷新内容并分配新 sequence，返回存储后的条目。"""

    def append(
        self,
        content: Any,
        *,
        kind: MemoryKind | str,
        scope: MemoryScope | str,
        agent_id: str | None = None,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """追加一条记忆，自动分配 entry_id 与 sequence。"""

    def update(
        self,
        entry_id: str,
        *,
        content: Any = UNSET,
        metadata: dict[str, Any] | None = UNSET,
    ) -> MemoryEntry | None:
        """局部更新条目；不存在返回 None。未提供的字段保持不变。"""

    def delete(self, entry_id: str) -> bool:
        """删除条目；删除成功返回 True，不存在返回 False。"""

    def query(self, spec: MemoryQuery) -> list[MemoryEntry]:
        """按过滤条件查询，按 sequence 排序后应用 offset/limit。"""

    def recent(
        self,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
        kind: MemoryKind | str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """返回最近 limit 条（按写入顺序倒序）。"""

    def current(
        self,
        key: str,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
    ) -> MemoryEntry | None:
        """返回指定 key 最近一次写入的条目；无匹配返回 None。"""

    def clear(self) -> None:
        """清空全部条目。"""

    def snapshot(self) -> list[MemoryEntry]:
        """返回当前全部条目的稳定快照。"""

    def stats(self) -> MemoryStats:
        """返回统计信息（总数、按 kind/scope 计数）。"""

    def close(self) -> None:
        """释放资源并关闭存储（幂等）。"""


@runtime_checkable
class MemoryReplaceAllPort(Protocol):
    """整集原子替换能力（语义记忆 / LLM 重写式记忆使用）。"""

    def replace_all(self, entries: list[MemoryEntry]) -> None:
        """用给定条目整体替换当前记忆集。"""


@runtime_checkable
class MemorySemanticSearchPort(Protocol):
    """语义检索能力（预留，后续接 embedding）。"""

    def semantic_search(
        self,
        text: str,
        *,
        limit: int = 10,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
    ) -> list[MemoryEntry]:
        """按语义相似度返回候选条目；未实现时抛 MemoryCapabilityError。"""


@runtime_checkable
class MemoryPersistencePort(Protocol):
    """显式 JSON 落盘能力（论文式存储；不自动保存）。"""

    def export_json(self, path: str) -> str:
        """导出全部条目到 JSON 文件，返回写入路径。"""

    def load_json(self, path: str) -> list[MemoryEntry]:
        """从 JSON 文件加载条目（覆盖当前内容）。"""


@runtime_checkable
class MemorySubscriptionPort(Protocol):
    """变更订阅能力（预留）。"""

    def subscribe(self, callback: EntryCallback) -> SubscriptionToken:
        """订阅写入事件，返回可退订的 token。"""

    def unsubscribe(self, token: SubscriptionToken) -> bool:
        """取消订阅；成功返回 True。"""


def require_capability(store: MemoryStore, capability: type) -> None:
    """校验存储是否支持指定能力，不支持时抛 MemoryCapabilityError。"""
    if not isinstance(store, capability):
        raise MemoryCapabilityError(
            f"Memory store {type(store).__name__} does not support "
            f"{capability.__name__}",
            operation="require_capability",
        )
