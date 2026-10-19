#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/memory 具体实现测试：InMemoryMemoryStore 与 TeamMemory（PPA 记忆）。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory import (  # noqa: E402
    InMemoryMemoryStore,
    MemoryEntry,
    MemoryError,
    MemoryKind,
    MemoryPersistencePort,
    MemoryRegistry,
    MemoryReplaceAllPort,
    MemoryScope,
    MemoryStore,
    TeamMemory,
    register_team_memory,
)

from memory_contract import MemoryStoreContractMixin  # noqa: E402


class InMemoryMemoryContractTests(MemoryStoreContractMixin, unittest.TestCase):
    """契约测试应用到 InMemoryMemoryStore。"""

    def make_store(self) -> MemoryStore:
        return InMemoryMemoryStore()


class TeamMemoryContractTests(MemoryStoreContractMixin, unittest.TestCase):
    """契约测试应用到 TeamMemory（验证透传与底层存储行为一致）。"""

    def make_store(self) -> MemoryStore:
        return TeamMemory.create()


class InMemoryMemoryCapabilityTests(unittest.TestCase):
    def test_declares_capabilities(self) -> None:
        store = InMemoryMemoryStore()
        self.assertIsInstance(store, MemoryStore)
        self.assertIsInstance(store, MemoryReplaceAllPort)
        self.assertIsInstance(store, MemoryPersistencePort)

    def test_replace_all_replaces_entries(self) -> None:
        store = InMemoryMemoryStore()
        store.append("old", kind=MemoryKind.TEAM_WORKING, scope="team")
        replacement = [
            MemoryEntry.create(
                MemoryKind.EPISODIC,
                MemoryScope.AGENT,
                "new",
                agent_id="g1",
            )
        ]
        store.replace_all(replacement)
        self.assertEqual(store.stats().total, 1)
        self.assertEqual(store.snapshot()[0].content, "new")

    def test_persistence_roundtrip(self) -> None:
        store = InMemoryMemoryStore()
        store.append(
            {"n": 1},
            kind=MemoryKind.TEAM_WORKING,
            scope=MemoryScope.TEAM,
            metadata={"tag": "x"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "memory.json")
            store.export_json(path)
            loaded = InMemoryMemoryStore()
            entries = loaded.load_json(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].content, {"n": 1})
            self.assertEqual(entries[0].metadata, {"tag": "x"})
            self.assertEqual(loaded.stats().total, 1)


class TeamMemoryObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = TeamMemory.create()

    def tearDown(self) -> None:
        self.memory.close()

    def test_update_observation_overwrites_same_entry(self) -> None:
        first = self.memory.update_observation(
            "g1",
            {"health": 20.0},
        )
        second = self.memory.update_observation(
            "g1",
            {"health": 10.0},
        )
        self.assertEqual(first.entry_id, second.entry_id)
        self.assertGreater(second.sequence, first.sequence)
        self.assertEqual(second.created_at, first.created_at)
        got = self.memory.get_observation("g1")
        self.assertEqual(got.content, {"health": 10.0})

    def test_observations_grouped_by_agent(self) -> None:
        self.memory.update_observation("g1", {"health": 10.0})
        self.memory.update_observation("g2", {"health": 20.0})
        observations = self.memory.observations()
        self.assertEqual(set(observations), {"g1", "g2"})
        self.assertEqual(observations["g1"].content, {"health": 10.0})

    def test_get_observation_missing_returns_none(self) -> None:
        self.assertIsNone(self.memory.get_observation("nobody"))


class TeamMemoryHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = TeamMemory.create()

    def tearDown(self) -> None:
        self.memory.close()

    def test_team_history_window(self) -> None:
        for index in range(5):
            self.memory.append_history_event(
                agent_id="g1", source="self", event_type="communication",
                content={"communication": f"msg{index}"},
            )
        recent = self.memory.prompt_history("g1", team_limit=3, system_limit=3)
        self.assertEqual(
            [entry["content"]["communication"] for entry in recent],
            ["msg2", "msg3", "msg4"],
        )

    def test_system_history_is_separate(self) -> None:
        self.memory.append_history_event(
            agent_id="system", source="system", event_type="system",
            content={"message": "system-1"},
        )
        self.memory.append_history_event(
            agent_id="g1", source="self", event_type="communication",
            content={"communication": "self-1"},
        )
        filtered = self.memory.prompt_history("g1", team_limit=1, system_limit=1)
        self.assertEqual(
            [entry["content"]["message"] for entry in filtered if entry["source"] == "system"],
            ["system-1"],
        )
        self.assertEqual(len(filtered), 2)

    def test_teammate_execution_history_is_public(self) -> None:
        self.memory.append_history_event(
            agent_id="g2", source="teammate", event_type="execution",
            content={"selected_action": "wait", "result_description": "done break", "result_status": "completed"},
        )
        recent = self.memory.prompt_history("g1", team_limit=5, system_limit=5)
        self.assertEqual(
            [entry["content"]["result_description"] for entry in recent],
            ["done break"],
        )

    def test_history_is_chronological_after_aggregation(self) -> None:
        self.memory.append_history_event(
            agent_id="g2", source="teammate", event_type="communication",
            content={"message": "first"},
        )
        self.memory.append_history_event(
            agent_id="system", source="system", event_type="system",
            content={"message": "second"},
        )
        messages = [entry["content"]["message"] for entry in self.memory.prompt_history("g1")]
        self.assertEqual(messages, ["first", "second"])

    def test_invalid_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.append_history_event(
                agent_id="g1", source="broadcast", event_type="communication", content={"message": "x"}
            )

    def test_invalid_limit_raises(self) -> None:
        with self.assertRaises(MemoryError):
            self.memory.prompt_history("g1", team_limit=0)


class TeamMemoryActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = TeamMemory.create()

    def tearDown(self) -> None:
        self.memory.close()

    def test_pending_action_history_and_update(self) -> None:
        first = self.memory.append_history_event(
            agent_id="g1", source="self", event_type="planning",
            content={"standard_plan": "combat.fight", "selected_action": "combat.fight", "reasoning": "engage"},
        )
        second = self.memory.append_history_event(
            agent_id="g1", source="self", event_type="planning",
            content={"standard_plan": "survival.use_food", "selected_action": "survival.use_food", "reasoning": "low health"},
        )
        history = self.memory.prompt_history("g1")
        self.assertEqual(history[-1]["entry_id"], second.entry_id)
        self.assertEqual(history[-1]["content"]["selected_action"], "survival.use_food")
        self.assertEqual(first.created_at, self.memory.get(first.entry_id).created_at)

    def test_update_history_preserves_planning_time(self) -> None:
        entry = self.memory.append_history_event(
            agent_id="g1", source="self", event_type="planning",
            content={"standard_plan": "inventory.list"},
        )
        completed = self.memory.update_history_event(
            entry.entry_id,
            content={"result_data": {"items": []}, "result_status": "completed"},
        )
        self.assertIsNotNone(completed)
        self.assertEqual(completed.content["result_status"], "completed")
        self.assertEqual(completed.content["result_data"], {"items": []})
        self.assertEqual(completed.sequence, entry.sequence)
        self.assertEqual(completed.created_at, entry.created_at)
        self.assertIsNone(self.memory.update_history_event("missing-id", content={"result_status": "completed"}))

    def test_action_history_chronological_and_filtered(self) -> None:
        self.memory.append_history_event(agent_id="g1", source="self", event_type="planning", content={"selected_action": "a1"})
        self.memory.append_history_event(agent_id="g1", source="self", event_type="planning", content={"selected_action": "a2"})
        self.memory.append_history_event(agent_id="g2", source="self", event_type="planning", content={"selected_action": "b1"})
        history_g1 = self.memory.prompt_history("g1")
        self.assertEqual(
            [entry["content"]["selected_action"] for entry in history_g1],
            ["a1", "a2"],
        )
        self.assertEqual(len(self.memory.snapshot()), 3)


class TeamMemoryPromptContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = TeamMemory.create(default_chat_window=3)

    def tearDown(self) -> None:
        self.memory.close()

    def test_prompt_context(self) -> None:
        self.memory.update_observation("g1", {"health": 10.0})
        self.memory.append_history_event(agent_id="g2", source="teammate", event_type="communication", content={"message": "need help"})
        self.memory.append_history_event(agent_id="g1", source="self", event_type="planning", content={"selected_action": "navigation.goto_position", "reasoning": "move"})
        context = self.memory.to_prompt_context("g1")
        self.assertEqual(context["agent_id"], "g1")
        self.assertEqual(context["observation"], {"health": 10.0})
        self.assertIsNotNone(context["observation_updated_at"])
        self.assertEqual(len(context["history"]), 2)
        self.assertEqual(context["history"][0]["source"], "teammate")
        self.assertEqual(context["history"][1]["content"]["selected_action"], "navigation.goto_position")

    def test_prompt_context_merges_messages_and_results_then_keeps_latest_window(self) -> None:
        for index in range(21):
            self.memory.append_history_event(agent_id="g1", source="self", event_type="communication", content={"communication": f"msg{index}"})
            self.memory.append_history_event(agent_id="g2", source="teammate", event_type="execution", content={"result_description": f"result{index}", "result_status": "completed"})

        context = self.memory.to_prompt_context("g1", chat_window=20)

        messages = [item["content"].get("communication") or item["content"].get("result_description") for item in context["history"]]
        self.assertEqual(len(messages), 20)
        self.assertEqual(messages[0], "msg11")
        self.assertEqual(messages[-1], "result20")
        self.assertNotIn("msg10", messages)
        self.assertNotIn("result10", messages)

    def test_default_prompt_context_window_is_twenty(self) -> None:
        memory = TeamMemory.create()
        try:
            self.assertEqual(memory.default_chat_window, 20)
        finally:
            memory.close()

    def test_prompt_context_uses_observation_renderer(self) -> None:
        self.memory.update_observation("g1", {"health": 10.0})
        context = self.memory.to_prompt_context(
            "g1",
            observation_renderer=lambda obs: f"health={obs['health']}",
        )
        self.assertEqual(context["observation"], "health=10.0")

    def test_prompt_context_missing_agent(self) -> None:
        context = self.memory.to_prompt_context("nobody")
        self.assertIsNone(context["observation"])
        self.assertEqual(context["history"], [])

    def test_invalid_default_chat_window_raises(self) -> None:
        with self.assertRaises(MemoryError):
            TeamMemory.create(default_chat_window=0)


class TeamMemoryPersistenceAndRegistryTests(unittest.TestCase):
    def test_team_memory_persistence_roundtrip(self) -> None:
        memory = TeamMemory.create()
        memory.update_observation("g1", {"health": 10.0})
        memory.append_history_event(agent_id="g1", source="self", event_type="communication", content={"communication": "hello"})
        memory.append_history_event(agent_id="g1", source="self", event_type="planning", content={"selected_action": "inventory.list", "result_status": "completed"})
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "team.json")
            memory.export_json(path)
            restored = TeamMemory.create()
            restored.load_json(path)
            self.assertEqual(
                restored.get_observation("g1").content,
                {"health": 10.0},
            )
            history = restored.prompt_history("g1")
            self.assertEqual(history[0]["content"]["communication"], "hello")
            self.assertEqual(history[1]["content"]["selected_action"], "inventory.list")

    def test_register_team_memory(self) -> None:
        registry = MemoryRegistry()
        register_team_memory(registry)
        self.assertIn("team_working", registry.available_kinds())
        memory = registry.create(MemoryKind.TEAM_WORKING)
        self.assertIsInstance(memory, TeamMemory)
        self.assertIsInstance(memory, MemoryStore)
        self.assertTrue(registry.supports("team_working", MemoryPersistencePort))
        self.assertTrue(registry.supports("team_working", MemoryReplaceAllPort))


if __name__ == "__main__":
    unittest.main()
