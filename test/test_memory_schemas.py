#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/memory 类型体系测试：条目/查询/统计/错误。"""

from __future__ import annotations

from pathlib import Path
import sys
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
    MemoryNotFoundError,
    MemoryQuery,
    MemoryScope,
    MemoryStats,
    UpdateSemantics,
)


class MemoryEntryTests(unittest.TestCase):
    def test_create_assigns_id_and_defaults(self) -> None:
        entry = MemoryEntry.create(
            "team_working", "team", {"a": 1}, agent_id="g1", key="obs"
        )
        self.assertTrue(entry.entry_id.startswith("mem-"))
        self.assertEqual(entry.kind, MemoryKind.TEAM_WORKING)
        self.assertEqual(entry.scope, MemoryScope.TEAM)
        self.assertEqual(entry.agent_id, "g1")
        self.assertEqual(entry.key, "obs")
        self.assertEqual(entry.sequence, 0)
        self.assertGreaterEqual(entry.updated_at, entry.created_at)

    def test_to_dict_from_dict_roundtrip(self) -> None:
        entry = MemoryEntry.create(
            MemoryKind.EPISODIC,
            MemoryScope.AGENT,
            {"n": 1},
            agent_id="g1",
            key="k",
            metadata={"tag": "x"},
        )
        restored = MemoryEntry.from_dict(entry.to_dict())
        self.assertEqual(restored, entry)

    def test_from_dict_coerces_string_enums(self) -> None:
        entry = MemoryEntry.from_dict(
            {
                "entry_id": "mem-1",
                "kind": "team_working",
                "scope": "team",
                "content": "hello",
            }
        )
        self.assertEqual(entry.kind, MemoryKind.TEAM_WORKING)
        self.assertEqual(entry.scope, MemoryScope.TEAM)

    def test_from_dict_missing_fields_raise(self) -> None:
        with self.assertRaises(MemoryError):
            MemoryEntry.from_dict({"entry_id": "mem-1"})

    def test_invalid_sequence_and_time_raise(self) -> None:
        with self.assertRaises(MemoryError):
            MemoryEntry(
                entry_id="mem-1",
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
                content="x",
                sequence=-1,
            )
        with self.assertRaises(MemoryError):
            MemoryEntry(
                entry_id="mem-1",
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
                content="x",
                created_at=10.0,
                updated_at=5.0,
            )

    def test_invalid_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            MemoryEntry.create("unknown_kind", MemoryScope.TEAM, "x")


class MemoryQueryTests(unittest.TestCase):
    def test_coerces_single_and_sequence(self) -> None:
        single = MemoryQuery(kinds="team_working", scopes=MemoryScope.AGENT)
        self.assertEqual(single.kinds, (MemoryKind.TEAM_WORKING,))
        self.assertEqual(single.scopes, (MemoryScope.AGENT,))
        multi = MemoryQuery(kinds=[MemoryKind.EPISODIC, "semantic"])
        self.assertEqual(
            multi.kinds,
            (MemoryKind.EPISODIC, MemoryKind.SEMANTIC),
        )

    def test_invalid_fields_raise(self) -> None:
        with self.assertRaises(MemoryError):
            MemoryQuery(order="sideways")
        with self.assertRaises(MemoryError):
            MemoryQuery(limit=0)
        with self.assertRaises(MemoryError):
            MemoryQuery(offset=-1)
        with self.assertRaises(MemoryError):
            MemoryQuery(since=10.0, until=5.0)

    def test_none_filters_stay_none(self) -> None:
        self.assertIsNone(MemoryQuery().kinds)
        self.assertIsNone(MemoryQuery().scopes)


class MemoryMiscTests(unittest.TestCase):
    def test_update_semantics_values(self) -> None:
        self.assertEqual(
            [item.value for item in UpdateSemantics],
            ["overwrite", "append", "window", "current", "replace_all"],
        )

    def test_stats_to_dict(self) -> None:
        stats = MemoryStats(total=2, by_kind={"team_working": 2})
        self.assertEqual(stats.to_dict()["total"], 2)

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(MemoryNotFoundError, MemoryError))
        self.assertTrue(issubclass(MemoryConflictError, MemoryError))
        self.assertTrue(issubclass(MemoryCapabilityError, MemoryError))
        error = MemoryError("boom", operation="test")
        self.assertEqual(error.operation, "test")


if __name__ == "__main__":
    unittest.main()
