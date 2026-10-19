"""通用内存记忆存储：线程安全，实现基础协议与持久化/整集替换能力。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .schemas import (
    MemoryEntry,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStats,
    UNSET,
)


class InMemoryMemoryStore:
    """线程安全的内存记忆存储。

    实现 MemoryStore 基础协议，并附带 MemoryReplaceAllPort 与
    MemoryPersistencePort 能力；供 TeamMemory 及后续各记忆形态复用。
    """

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._sequence = 0
        self._lock = threading.RLock()

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def get(self, entry_id: str) -> MemoryEntry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def put(self, entry: MemoryEntry) -> MemoryEntry:
        with self._lock:
            existing = self._entries.get(entry.entry_id)
            now = time.time()
            stored = MemoryEntry(
                entry_id=entry.entry_id,
                kind=entry.kind,
                scope=entry.scope,
                content=entry.content,
                agent_id=entry.agent_id,
                key=entry.key,
                # A history lifecycle update must not move the event in time.
                sequence=(
                    existing.sequence
                    if existing is not None and existing.key == "history"
                    else self._next_sequence()
                ),
                created_at=existing.created_at if existing else entry.created_at,
                updated_at=now,
                metadata=dict(entry.metadata),
            )
            self._entries[stored.entry_id] = stored
            return stored

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
        with self._lock:
            entry = MemoryEntry.create(
                kind,
                scope,
                content,
                agent_id=agent_id,
                key=key,
                metadata=metadata,
            )
            entry.sequence = self._next_sequence()
            self._entries[entry.entry_id] = entry
            return entry

    def update(
        self,
        entry_id: str,
        *,
        content: Any = UNSET,
        metadata: dict[str, Any] | None = UNSET,
    ) -> MemoryEntry | None:
        with self._lock:
            existing = self._entries.get(entry_id)
            if existing is None:
                return None
            stored = MemoryEntry(
                entry_id=existing.entry_id,
                kind=existing.kind,
                scope=existing.scope,
                content=existing.content if content is UNSET else content,
                agent_id=existing.agent_id,
                key=existing.key,
                sequence=(
                    existing.sequence
                    if existing.key == "history"
                    else self._next_sequence()
                ),
                created_at=existing.created_at,
                updated_at=time.time(),
                metadata=(
                    existing.metadata
                    if metadata is UNSET
                    else dict(metadata or {})
                ),
            )
            self._entries[entry_id] = stored
            return stored

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            return self._entries.pop(entry_id, None) is not None

    def query(self, spec: MemoryQuery) -> list[MemoryEntry]:
        with self._lock:
            items = list(self._entries.values())
            if spec.kinds is not None:
                items = [e for e in items if e.kind in spec.kinds]
            if spec.scopes is not None:
                items = [e for e in items if e.scope in spec.scopes]
            if spec.agent_id is not None:
                items = [e for e in items if e.agent_id == spec.agent_id]
            if spec.key is not None:
                items = [e for e in items if e.key == spec.key]
            if spec.since is not None:
                items = [e for e in items if e.created_at >= spec.since]
            if spec.until is not None:
                items = [e for e in items if e.created_at <= spec.until]
            items.sort(key=lambda e: e.sequence, reverse=(spec.order == "desc"))
            if spec.offset:
                items = items[spec.offset:]
            if spec.limit is not None:
                items = items[: spec.limit]
            return items

    def recent(
        self,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
        kind: MemoryKind | str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        with self._lock:
            return self.query(
                MemoryQuery(
                    kinds=kind,
                    scopes=scope,
                    agent_id=agent_id,
                    limit=limit,
                    order="desc",
                )
            )

    def current(
        self,
        key: str,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
    ) -> MemoryEntry | None:
        with self._lock:
            items = self.query(
                MemoryQuery(key=key, scopes=scope, agent_id=agent_id, limit=1)
            )
            return items[0] if items else None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def snapshot(self) -> list[MemoryEntry]:
        with self._lock:
            return [self._entries[key] for key in sorted(self._entries)]

    def stats(self) -> MemoryStats:
        with self._lock:
            by_kind: dict[str, int] = {}
            by_scope: dict[str, int] = {}
            for entry in self._entries.values():
                by_kind[entry.kind.value] = by_kind.get(entry.kind.value, 0) + 1
                by_scope[entry.scope.value] = by_scope.get(entry.scope.value, 0) + 1
            return MemoryStats(
                total=len(self._entries),
                by_kind=by_kind,
                by_scope=by_scope,
            )

    def close(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------
    # 能力: MemoryReplaceAllPort / MemoryPersistencePort
    # ------------------------------------------------------------------

    def replace_all(self, entries: list[MemoryEntry]) -> None:
        with self._lock:
            self._entries = {
                entry.entry_id: MemoryEntry(
                    entry_id=entry.entry_id,
                    kind=entry.kind,
                    scope=entry.scope,
                    content=entry.content,
                    agent_id=entry.agent_id,
                    key=entry.key,
                    sequence=self._next_sequence(),
                    created_at=entry.created_at,
                    updated_at=entry.updated_at,
                    metadata=dict(entry.metadata),
                )
                for entry in entries
            }

    def export_json(self, path: str) -> str:
        with self._lock:
            payload = [entry.to_dict() for entry in self._entries.values()]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(target)

    def load_json(self, path: str) -> list[MemoryEntry]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [MemoryEntry.from_dict(item) for item in payload]
        with self._lock:
            self._entries = {entry.entry_id: entry for entry in entries}
            self._sequence = max(
                (entry.sequence for entry in entries),
                default=0,
            )
        return entries
