"""记忆存储行为契约测试 mixin。

未来的记忆实现只需继承 MemoryStoreContractMixin（与 unittest.TestCase 组合时
mixin 在前）并实现 make_store()，即可复用统一的契约测试，保证不同记忆模块行为一致。
本文件为 mixin（不继承 TestCase），不会参与 unittest 发现。
"""

from __future__ import annotations

from src.memory import MemoryEntry, MemoryKind, MemoryQuery, MemoryScope, MemoryStore


class MemoryStoreContractMixin:
    """MemoryStore 行为契约（需与 unittest.TestCase 组合使用）。"""

    def make_store(self) -> MemoryStore:
        raise NotImplementedError("subclass must implement make_store()")

    def setUp(self) -> None:
        self.store = self.make_store()

    def tearDown(self) -> None:
        self.store.close()

    def test_put_then_get_roundtrip(self) -> None:
        entry = MemoryEntry.create(
            MemoryKind.TEAM_WORKING,
            MemoryScope.TEAM,
            {"a": 1},
            agent_id="g1",
        )
        stored = self.store.put(entry)
        got = self.store.get(stored.entry_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.content, {"a": 1})
        self.assertEqual(got.kind, MemoryKind.TEAM_WORKING)
        self.assertEqual(got.scope, MemoryScope.TEAM)
        self.assertGreaterEqual(got.updated_at, got.created_at)

    def test_put_updates_existing_entry_and_sequence_increases(self) -> None:
        first = self.store.append(
            "v1",
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.TEAM,
        )
        updated = self.store.put(
            MemoryEntry(
                entry_id=first.entry_id,
                kind=first.kind,
                scope=first.scope,
                content="v2",
                created_at=first.created_at,
            )
        )
        self.assertGreater(updated.sequence, first.sequence)
        got = self.store.get(first.entry_id)
        self.assertEqual(got.content, "v2")
        self.assertEqual(got.created_at, first.created_at)

    def test_append_assigns_unique_ids_and_increasing_sequences(self) -> None:
        ids: list[str] = []
        sequences: list[int] = []
        for index in range(3):
            entry = self.store.append(
                {"n": index},
                kind=MemoryKind.EPISODIC,
                scope=MemoryScope.AGENT,
                agent_id="g1",
            )
            ids.append(entry.entry_id)
            sequences.append(entry.sequence)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len(set(sequences)), 3)
        self.assertEqual(sequences, sorted(sequences))

    def test_recent_returns_latest_n_in_desc_order(self) -> None:
        for index in range(5):
            self.store.append(
                f"m{index}",
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
            )
        recent = self.store.recent(limit=3)
        self.assertEqual([entry.content for entry in recent], ["m4", "m3", "m2"])

    def test_current_returns_latest_entry_for_key(self) -> None:
        self.store.append(
            {"step": 1},
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.AGENT,
            agent_id="g1",
            key="obs",
        )
        self.store.append(
            {"step": 2},
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.AGENT,
            agent_id="g1",
            key="obs",
        )
        current = self.store.current("obs", agent_id="g1")
        self.assertEqual(current.content, {"step": 2})
        self.assertIsNone(self.store.current("missing"))

    def test_query_filters_by_kind_agent_and_orders(self) -> None:
        self.store.append(
            "a1",
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.AGENT,
            agent_id="g1",
        )
        self.store.append(
            "a2",
            kind=MemoryKind.EPISODIC,
            scope=MemoryScope.AGENT,
            agent_id="g1",
        )
        self.store.append(
            "b1",
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.AGENT,
            agent_id="g2",
        )
        result = self.store.query(
            MemoryQuery(
                kinds=MemoryKind.TEAM_WORKING,
                agent_id="g1",
                order="asc",
            )
        )
        self.assertEqual([entry.content for entry in result], ["a1"])

    def test_query_applies_limit_and_offset(self) -> None:
        for index in range(5):
            self.store.append(
                f"m{index}",
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
            )
        page = self.store.query(
            MemoryQuery(order="desc", offset=1, limit=2)
        )
        self.assertEqual([entry.content for entry in page], ["m3", "m2"])

    def test_delete_removes_entry(self) -> None:
        entry = self.store.append(
            "x",
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.TEAM,
        )
        self.assertTrue(self.store.delete(entry.entry_id))
        self.assertIsNone(self.store.get(entry.entry_id))
        self.assertFalse(self.store.delete(entry.entry_id))

    def test_update_changes_content_and_metadata(self) -> None:
        entry = self.store.append(
            "old",
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.TEAM,
            metadata={"tag": "a"},
        )
        updated = self.store.update(
            entry.entry_id,
            content="new",
            metadata={"tag": "b"},
        )
        self.assertIsNotNone(updated)
        got = self.store.get(entry.entry_id)
        self.assertEqual(got.content, "new")
        self.assertEqual(got.metadata, {"tag": "b"})
        self.assertGreater(got.sequence, entry.sequence)
        self.assertIsNone(
            self.store.update("missing-id", content="nope")
        )

    def test_clear_empties_store(self) -> None:
        self.store.append("x", kind=MemoryKind.TEAM_WORKING, scope=MemoryScope.TEAM)
        self.store.clear()
        self.assertEqual(self.store.snapshot(), [])
        self.assertEqual(self.store.stats().total, 0)

    def test_snapshot_contains_all_entries(self) -> None:
        ids = [
            self.store.append(
                f"m{index}",
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
            ).entry_id
            for index in range(3)
        ]
        snapshot_ids = [entry.entry_id for entry in self.store.snapshot()]
        self.assertEqual(sorted(snapshot_ids), sorted(ids))

    def test_stats_counts_by_kind(self) -> None:
        self.store.append("a", kind=MemoryKind.TEAM_WORKING, scope=MemoryScope.TEAM)
        self.store.append("b", kind=MemoryKind.EPISODIC, scope=MemoryScope.AGENT)
        self.store.append("c", kind=MemoryKind.EPISODIC, scope=MemoryScope.AGENT)
        stats = self.store.stats()
        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.by_kind["team_working"], 1)
        self.assertEqual(stats.by_kind["episodic"], 2)
        self.assertEqual(stats.by_scope["agent"], 2)
