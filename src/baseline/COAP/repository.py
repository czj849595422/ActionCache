"""COAP 缓存模块的内存存储实现（同步、线程安全）。"""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Sequence

from .ports import RepositoryPortError
from .schemas import CacheEntry


class InMemoryCacheRepository:
    """基于字典的线程安全缓存仓库。"""

    def __init__(self, entries: Sequence[CacheEntry] = ()) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, CacheEntry] = {}
        self._replace_all_unlocked(entries)

    def _replace_all_unlocked(self, entries: Sequence[CacheEntry]) -> None:
        if isinstance(entries, (str, bytes)) or not isinstance(
            entries, (tuple, list)
        ):
            raise RepositoryPortError(
                "entries 必须是序列", operation="replace_all"
            )
        result: dict[str, CacheEntry] = {}
        for entry in entries:
            if not isinstance(entry, CacheEntry):
                raise RepositoryPortError(
                    "entries 必须包含 CacheEntry", operation="replace_all"
                )
            if entry.entry_id in result:
                raise RepositoryPortError(
                    f"重复的 cache entry_id: {entry.entry_id}",
                    operation="replace_all",
                )
            result[entry.entry_id] = deepcopy(entry)
        self._entries = result

    def list_entries(
        self,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        match_owner: bool = False,
    ) -> tuple[CacheEntry, ...]:
        with self._lock:
            entries = (
                entry
                for entry in self._entries.values()
                if task_scope is None or entry.task_scope == task_scope
                if not match_owner or entry.owner_agent_id == owner_agent_id
            )
            return tuple(
                deepcopy(entry)
                for entry in sorted(entries, key=lambda item: item.entry_id)
            )

    def get(self, entry_id: str) -> CacheEntry | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            return None if entry is None else deepcopy(entry)

    def find_by_previous_key(
        self,
        canonical_key: str,
        *,
        task_scope: str | None = None,
        owner_agent_id: str | None = None,
        match_owner: bool = False,
    ) -> tuple[CacheEntry, ...]:
        with self._lock:
            entries = (
                entry
                for entry in self._entries.values()
                if entry.previous_action.canonical_key == canonical_key
                and (task_scope is None or entry.task_scope == task_scope)
                and (not match_owner or entry.owner_agent_id == owner_agent_id)
            )
            return tuple(
                deepcopy(entry)
                for entry in sorted(entries, key=lambda item: item.entry_id)
            )

    def upsert(self, entry: CacheEntry) -> None:
        if not isinstance(entry, CacheEntry):
            raise RepositoryPortError("entry 必须是 CacheEntry", operation="upsert")
        with self._lock:
            self._entries[entry.entry_id] = deepcopy(entry)

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            return self._entries.pop(entry_id, None) is not None

    def replace_all(self, entries: Sequence[CacheEntry]) -> None:
        with self._lock:
            self._replace_all_unlocked(entries)


__all__ = ["InMemoryCacheRepository"]
