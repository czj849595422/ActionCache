#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/memory 统一接口测试：协议契约、能力校验与注册表。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory import (  # noqa: E402
    MemoryCapabilityError,
    MemoryConflictError,
    MemoryEntry,
    MemoryError,
    MemoryKind,
    MemoryPersistencePort,
    MemoryQuery,
    MemoryRegistry,
    MemoryScope,
    MemoryStats,
    MemoryStore,
    UNSET,
    require_capability,
)

from memory_contract import MemoryStoreContractMixin  # noqa: E402


class FakeMemoryStore:
    """最小 MemoryStore 实现（线程安全，用于契约与协议测试）。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._sequence = 0
        self._lock = threading.RLock()
        self._closed = False

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
                sequence=self._next_sequence(),
                created_at=existing.created_at if existing else entry.created_at,
                updated_at=now,
                metadata=dict(entry.metadata),
            )
            self._entries[stored.entry_id] = stored
            return stored

    def append(
        self,
        content: object,
        *,
        kind: MemoryKind | str,
        scope: MemoryScope | str,
        agent_id: str | None = None,
        key: str | None = None,
        metadata: dict[str, object] | None = None,
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
        content: object = UNSET,
        metadata: dict[str, object] | None = UNSET,
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
                sequence=self._next_sequence(),
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
            self._closed = True


class FakePersistentMemoryStore(FakeMemoryStore, MemoryPersistencePort):
    """额外实现 MemoryPersistencePort 的存储，用于能力校验测试。"""

    def export_json(self, path: str) -> str:
        with self._lock:
            payload = [entry.to_dict() for entry in self._entries.values()]
        target = Path(path)
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


class FakeMemoryContractTests(MemoryStoreContractMixin, unittest.TestCase):
    """契约测试应用到 FakeMemoryStore。"""

    def make_store(self) -> MemoryStore:
        return FakeMemoryStore()


class MemoryStoreProtocolTests(unittest.TestCase):
    def test_fake_implements_memory_store_protocol(self) -> None:
        self.assertIsInstance(FakeMemoryStore(), MemoryStore)

    def test_capability_not_implemented_raises(self) -> None:
        store = FakeMemoryStore()
        self.assertNotIsInstance(store, MemoryPersistencePort)
        with self.assertRaises(MemoryCapabilityError):
            require_capability(store, MemoryPersistencePort)

    def test_capability_implemented_passes(self) -> None:
        store = FakePersistentMemoryStore()
        self.assertIsInstance(store, MemoryPersistencePort)
        require_capability(store, MemoryPersistencePort)

    def test_persistence_roundtrip(self) -> None:
        store = FakePersistentMemoryStore()
        store.append("x", kind=MemoryKind.TEAM_WORKING, scope=MemoryScope.TEAM)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            store.export_json(str(path))
            loaded = FakePersistentMemoryStore()
            entries = loaded.load_json(str(path))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].content, "x")


class MemoryRegistryTests(unittest.TestCase):
    def test_register_create_and_available(self) -> None:
        registry = MemoryRegistry()
        registry.register(MemoryKind.TEAM_WORKING, FakeMemoryStore)
        self.assertEqual(registry.available_kinds(), ["team_working"])
        store = registry.create("team_working")
        self.assertIsInstance(store, FakeMemoryStore)

    def test_create_unknown_kind_raises(self) -> None:
        registry = MemoryRegistry()
        with self.assertRaises(MemoryError):
            registry.create("episodic")

    def test_duplicate_register_raises_without_overwrite(self) -> None:
        registry = MemoryRegistry()
        registry.register(MemoryKind.TEAM_WORKING, FakeMemoryStore)
        with self.assertRaises(MemoryConflictError):
            registry.register(MemoryKind.TEAM_WORKING, FakeMemoryStore)
        registry.register(
            MemoryKind.TEAM_WORKING,
            FakePersistentMemoryStore,
            overwrite=True,
        )
        self.assertIsInstance(
            registry.create(MemoryKind.TEAM_WORKING),
            FakePersistentMemoryStore,
        )

    def test_non_callable_factory_raises(self) -> None:
        registry = MemoryRegistry()
        with self.assertRaises(MemoryError):
            registry.register(MemoryKind.TEAM_WORKING, "not-callable")

    def test_supports_capability(self) -> None:
        registry = MemoryRegistry()
        registry.register(MemoryKind.TEAM_WORKING, FakeMemoryStore)
        registry.register(
            MemoryKind.EPISODIC,
            FakePersistentMemoryStore,
            capabilities=(MemoryPersistencePort,),
        )
        self.assertFalse(
            registry.supports(MemoryKind.TEAM_WORKING, MemoryPersistencePort)
        )
        self.assertTrue(
            registry.supports(MemoryKind.EPISODIC, MemoryPersistencePort)
        )
        self.assertFalse(registry.supports("unknown", MemoryPersistencePort))


if __name__ == "__main__":
    unittest.main()
