from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.agents.action import ActionMode, ActionResult, ActionStatus, SkillRegistry
from src.baseline.AgenticCache import (
    ActionPlan,
    AgenticCacheConfig,
    AgenticCacheDecisionCodec,
    AgenticCacheProposal,
    AgenticCacheRunner,
    AgenticCacheStore,
    build_agentic_cache_instructions,
)
from src.baseline.AgenticCache.metadata import metadata_from_observation
from src.baseline.AgenticCache.protocol import parse_action_response
from src.baseline.AgenticCache.templates import instantiate_template_plan
from src.baseline.AgenticCache.workers import (
    AgenticCacheActingWorker,
    AgenticCacheCoordinator,
    AgenticCachePlanningWorker,
)
from src.baseline.AgenticCache.prompts import (
    AgenticCachePromptBuilder,
    build_planning_request,
)
from src.baseline.AgenticCache.budget import RunBudget
from src.interface import ActionUnit
from src.llm import LLMConfig
from src.memory import TeamMemory
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer


class AgenticCacheStoreTests(unittest.TestCase):
    def test_lookup_filters_by_metadata_range_and_scores(self) -> None:
        cache = AgenticCacheStore(top_k=2)
        start = ActionPlan("START")
        first = ActionPlan("inventory")
        second = ActionPlan("inspect_inventory")
        cache.upsert_success(
            previous_action=start,
            next_action=first,
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
            reason="first",
        )
        cache.upsert_success(
            previous_action=start,
            next_action=second,
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
            reason="second",
        )
        cache.upsert_success(
            previous_action=start,
            next_action=second,
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
            reason="second again",
        )

        hit = cache.lookup(start, {"action_index": 0}, owner_agent_id="Alice")
        self.assertEqual([item.next_action.name for item in hit.entries], [
            "inspect_inventory",
            "inventory",
        ])

        miss = cache.lookup(start, {"action_index": 3}, owner_agent_id="Alice")
        self.assertFalse(miss.hit)
        self.assertEqual(miss.filtered_count, 2)

    def test_upsert_expands_metadata_range(self) -> None:
        cache = AgenticCacheStore()
        start = ActionPlan("START")
        action = ActionPlan("inventory")
        cache.upsert_success(
            previous_action=start,
            next_action=action,
            state_metadata={"elapsed_seconds": 1},
            owner_agent_id="Alice",
        )
        cache.upsert_success(
            previous_action=start,
            next_action=action,
            state_metadata={"elapsed_seconds": 5},
            owner_agent_id="Alice",
        )

        hit = cache.lookup(start, {"elapsed_seconds": 3}, owner_agent_id="Alice")
        self.assertTrue(hit.hit)
        self.assertEqual(hit.entries[0].transition_count, 2)

    def test_snapshot_exports_v2_with_entry_count(self) -> None:
        cache = AgenticCacheStore()
        cache.upsert_success(
            previous_action=ActionPlan("START"),
            next_action=ActionPlan("inventory"),
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
        )

        payload = cache.to_dict()

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["entry_count"], 1)
        self.assertEqual(payload["entries"][0]["next_action"]["name"], "inventory")

    def test_load_v1_cache_source_migrates_canonical_actions(self) -> None:
        payload = {
            "schema_version": 1,
            "kind": "agentic_cache",
            "entries": [
                {
                    "previous_action": {"name": "START", "parameters": {}},
                    "next_action": {
                        "name": "navigation.goto_position",
                        "parameters": {"x": 10, "y": 0, "z": 10},
                    },
                    "transition_count": 1,
                    "candidate_count": 1,
                    "confirmed_count": 1,
                    "failure_count": 0,
                    "owner_agent_id": "Alice",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cache = AgenticCacheStore(
                instructions=build_agentic_cache_instructions()
            )
            loaded = cache.load_file(path)

        entries = cache.list_entries(owner_agent_id="Alice")
        self.assertEqual(loaded, 1)
        self.assertEqual(entries[0].next_action.name, "goto")
        self.assertIn("position_bucket", entries[0].next_action.parameters)


class AgenticCacheProtocolTests(unittest.TestCase):
    def test_parse_action_response(self) -> None:
        parsed = parse_action_response(
            "<reason>检查背包。</reason><action>inventory()</action><chat>我先检查背包</chat>"
        )
        self.assertEqual(parsed.reasoning, "检查背包。")
        self.assertEqual(parsed.action_text, "inventory()")
        self.assertEqual(parsed.communication, "我先检查背包")

    def test_codec_rejects_invalid_cached_params(self) -> None:
        registry = SkillRegistry(allowed_actions={"inventory.count_item"})
        codec = AgenticCacheDecisionCodec(registry, build_agentic_cache_instructions())
        with self.assertRaises(ValueError):
            codec.from_cached_plan(
                ActionPlan("count_item", {}),
                agent_id="Alice",
                previous_action=ActionPlan("START"),
                state_metadata={},
                cache_entry_id="entry",
            )

    def test_llm_fight_shorthand_creates_rebindable_template(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.fight"})
        codec = AgenticCacheDecisionCodec(registry, build_agentic_cache_instructions())

        proposal = codec.parse_complete(
            "<reason>攻击最近僵尸。</reason><action>fight(zombie_65)</action>",
            agent_id="Alice",
        )

        self.assertEqual(proposal.action_unit.action, "combat.fight")
        self.assertEqual(proposal.action_unit.params["target"]["entity_id"], 65)
        self.assertIsNotNone(proposal.cache_action)
        self.assertEqual(proposal.cache_action.name, "fight")
        self.assertEqual(
            proposal.cache_action.parameters,
            {"target_selector": "nearest_hostile", "kind": "zombie"},
        )

    def test_codec_rejects_canonical_action_text(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.fight"})
        codec = AgenticCacheDecisionCodec(registry, build_agentic_cache_instructions())

        with self.assertRaises(ValueError):
            codec.parse_complete(
                "<reason>bad</reason><action>combat.fight(target='zombie')</action>",
                agent_id="Alice",
            )

    def test_goto_template_buckets_nearby_positions(self) -> None:
        registry = SkillRegistry(allowed_actions={"navigation.goto_position"})
        codec = AgenticCacheDecisionCodec(registry, build_agentic_cache_instructions())

        first = codec.parse_complete(
            "<reason>move</reason><action>goto(position={'x':10,'y':0,'z':10})</action>",
            agent_id="Alice",
        )
        second = codec.parse_complete(
            "<reason>move</reason><action>goto(position={'x':12,'y':0,'z':12})</action>",
            agent_id="Alice",
        )

        self.assertEqual(first.cache_action.parameters, second.cache_action.parameters)


class AgenticCachePromptTests(unittest.TestCase):
    def test_planning_prompt_includes_all_current_failure_feedback(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "ready")
        request = build_planning_request(
            prompt_builder=AgenticCachePromptBuilder(),
            memory=memory,
            spec=SimpleNamespace(description="test", completion={}),
            registry=SkillRegistry(allowed_actions={"combat.observe"}),
            instructions=build_agentic_cache_instructions(),
            config=AgenticCacheConfig(),
            agent_id="Alice",
            teammates=(),
            last_error="latest failure",
            error_history=("first failure", "second failure"),
            cache_context="缓存未命中",
        )

        user = request.messages[1]["content"]
        self.assertIn("第 1 次失败：first failure", user)
        self.assertIn("第 2 次失败：second failure", user)
        self.assertNotIn("latest failure", user)

    def test_completed_action_result_description_enters_current_action(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "ready")
        entry = memory.append_history_event(
            agent_id="Alice", source="self", event_type="planning",
            content={"standard_plan": "combat.observe", "selected_action": "combat.observe", "reasoning": "scan"},
        )
        memory.update_history_event(
            entry.entry_id,
            content={
                "result_data": {"action": "combat.observe", "ok": True},
                "result_description": "侦查完成：发现 Eli 在 (1, 64, 2)。",
                "result_status": "completed",
            },
        )

        request = build_planning_request(
            prompt_builder=AgenticCachePromptBuilder(),
            memory=memory,
            spec=SimpleNamespace(
                task_id="pvp",
                load_description_text=lambda: "PVP test",
            ),
            registry=SkillRegistry(allowed_actions={"combat.observe"}),
            instructions=build_agentic_cache_instructions(),
            config=AgenticCacheConfig(),
            agent_id="Alice",
            teammates=["Blake"],
            last_error=None,
            cache_context="缓存未命中",
        )

        user = request.messages[1]["content"]
        self.assertIn("【历史信息】", user)
        self.assertIn("普通规划：combat.observe", user)
        self.assertIn("状态：completed", user)
        self.assertIn("侦查完成：发现 Eli 在 (1, 64, 2)。", user)

    def test_result_channel_enters_team_messages(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "ready")
        memory.append_history_event(
            agent_id="Bob", source="teammate", event_type="execution",
            content={"selected_action": "pickup", "result_description": "picked arrows", "result_status": "completed"},
        )
        memory.append_history_event(
            agent_id="Bob", source="teammate", event_type="communication",
            content={"message": "moving east"},
        )

        request = build_planning_request(
            prompt_builder=AgenticCachePromptBuilder(),
            memory=memory,
            spec=SimpleNamespace(
                task_id="pvp",
                load_description_text=lambda: "PVP test",
            ),
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
            instructions=build_agentic_cache_instructions(),
            config=AgenticCacheConfig(chat_window=20),
            agent_id="Alice",
            teammates=["Bob"],
            last_error=None,
            cache_context="缓存未命中",
        )

        user = request.messages[1]["content"]
        self.assertIn("动作完成结果：pickup（状态：completed）", user)
        self.assertIn("通信：moving east", user)


class AgenticCacheMetadataTests(unittest.TestCase):
    def test_extracts_bounded_numeric_task_metadata(self) -> None:
        observation = {
            "task": {"facts": [{"elapsed_seconds": 2, "alive_monsters": 3}]},
            "sections": {
                "hostile_entities": {
                    "facts": [{}, {}, {}],
                    "summary": {"returned_fact_count": 3},
                }
            },
        }
        result = metadata_from_observation(observation, max_fields=8)
        self.assertEqual(result["task.alive_monsters"], 3.0)
        self.assertEqual(result["section.hostile_entities.fact_count"], 3.0)


class AgenticCacheWorkerTests(unittest.TestCase):
    def test_planning_failures_are_retried_after_threshold(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.observe"})
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {},
            },
        )
        llm = _SequenceLLM(
            [
                "invalid-1",
                "invalid-2",
                "invalid-3",
                "<reason>recovered</reason><action>observe()</action>",
            ]
        )
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=llm,
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=SingleSlotOverwriteBuffer(),
            memory=memory,
            config=AgenticCacheConfig(max_consecutive_errors=3),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=AgenticCacheStore(),
            coordinator=AgenticCacheCoordinator(),
            acting_worker=SimpleNamespace(is_busy=False, seen_version=0),
        )

        for _ in range(4):
            worker.iterate()

        self.assertEqual(llm.calls, 4)
        self.assertEqual(worker.planned_count, 1)
        self.assertEqual(worker.consecutive_errors, 0)
        self.assertEqual(worker.planning_error_history, [])

    def test_planning_worker_uses_cache_hit_without_llm(self) -> None:
        registry = SkillRegistry(allowed_actions={"inventory.list"})
        cache = AgenticCacheStore()
        cache.upsert_success(
            previous_action=ActionPlan("START"),
            next_action=ActionPlan("inventory"),
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
        )
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {},
            },
        )
        buffer = SingleSlotOverwriteBuffer()
        coordinator = AgenticCacheCoordinator()
        llm = _FailingLLM()
        acting = SimpleNamespace(is_busy=False, seen_version=0)
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=llm,
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=cache,
            coordinator=coordinator,
            acting_worker=acting,
        )

        worker.iterate()

        latest = buffer.peek()
        self.assertIsNotNone(latest)
        proposal = latest[1]
        self.assertEqual(proposal.path, "cache")
        self.assertEqual(proposal.action_unit.action, "inventory.list")
        self.assertEqual(llm.calls, 0)

    def test_cache_hit_rebinds_combat_target_from_current_observation(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.fight"})
        cache = AgenticCacheStore()
        cache.upsert_success(
            previous_action=ActionPlan("START"),
            next_action=ActionPlan(
                "fight",
                {"target_selector": "nearest_hostile", "kind": "zombie"},
            ),
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
        )
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {
                    "hostile_entities": [
                        {
                            "kind": "zombie",
                            "entity_id": 99,
                            "distance": 3,
                            "position": {"x": 1, "y": 0, "z": 2},
                        }
                    ]
                },
            },
        )
        buffer = SingleSlotOverwriteBuffer()
        coordinator = AgenticCacheCoordinator()
        llm = _FailingLLM()
        acting = SimpleNamespace(is_busy=False, seen_version=0)
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=llm,
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=cache,
            coordinator=coordinator,
            acting_worker=acting,
        )

        worker.iterate()

        proposal = buffer.peek()[1]
        self.assertEqual(proposal.path, "cache")
        self.assertEqual(proposal.action_unit.action, "combat.fight")
        self.assertEqual(proposal.action_unit.params["target"]["entity_id"], 99)
        self.assertEqual(llm.calls, 0)

    def test_cached_boss_target_keeps_compact_numeric_id(self) -> None:
        _, params, _, _, _, _ = instantiate_template_plan(
            ActionPlan(
                "fight",
                {"target_selector": "nearest_hostile", "kind": "wither"},
            ),
            binding_context={
                "hostile_entities": [
                    {
                        "kind": "wither",
                        "roles": ["boss"],
                        "entity_id": 70,
                        "uuid": "73479e72-1111-2222-3333-444444444444",
                        "distance": 3,
                    }
                ]
            },
            instructions=build_agentic_cache_instructions(),
        )

        self.assertEqual(params["target"], {"name": "wither", "entity_id": 70})

    def test_cache_candidate_rebind_failure_falls_back_to_llm(self) -> None:
        registry = SkillRegistry(
            allowed_actions={"combat.fight", "combat.observe"}
        )
        cache = AgenticCacheStore()
        cache_entry = cache.upsert_success(
            previous_action=ActionPlan("START"),
            next_action=ActionPlan(
                "fight",
                {"target_selector": "nearest_hostile", "kind": "zombie"},
            ),
            state_metadata={"action_index": 0},
            owner_agent_id="Alice",
        )
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {
                    "hostile_entities": [
                        {
                            "kind": "skeleton",
                            "entity_id": 100,
                            "distance": 3,
                            "position": {"x": 1, "y": 0, "z": 2},
                        }
                    ]
                },
            },
        )
        buffer = SingleSlotOverwriteBuffer()
        coordinator = AgenticCacheCoordinator()
        llm = _StaticLLM("<reason>观察。</reason><action>observe()</action>")
        acting = SimpleNamespace(is_busy=False, seen_version=0)
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=llm,
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=cache,
            coordinator=coordinator,
            acting_worker=acting,
        )

        worker.iterate()

        proposal = buffer.peek()[1]
        self.assertEqual(proposal.path, "llm")
        self.assertEqual(proposal.action_unit.action, "combat.observe")
        rejected = cache.list_entries(owner_agent_id="Alice")[0]
        self.assertEqual(rejected.entry_id, cache_entry.entry_id)
        self.assertEqual(rejected.failure_count, 1)
        self.assertEqual(llm.calls, 1)

    def test_executable_plan_chat_enters_memory_before_action_finishes(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.observe"})
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {},
            },
        )
        buffer = SingleSlotOverwriteBuffer()
        coordinator = AgenticCacheCoordinator()
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=_StaticLLM(
                "<reason>scan</reason><action>observe()</action><chat>scan now</chat>"
            ),
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=AgenticCacheStore(),
            coordinator=coordinator,
            acting_worker=SimpleNamespace(is_busy=False, seen_version=0),
        )

        worker.iterate()

        self.assertIsNotNone(buffer.peek())
        active_chats = memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertEqual(
            [item["content"].get("communication") for item in active_chats if item["content"].get("communication")],
            ["scan now"],
        )

    def test_planning_worker_throttles_when_llm_budget_exhausted(self) -> None:
        registry = SkillRegistry(allowed_actions={"combat.observe"})
        memory = TeamMemory()
        memory.update_observation(
            "Alice",
            "obs",
            metadata={
                "agentic_cache_metadata": {},
                "agentic_cache_binding_context": {},
            },
        )
        buffer = SingleSlotOverwriteBuffer()
        coordinator = AgenticCacheCoordinator()
        llm = _FailingLLM()
        events = []
        budget = RunBudget(team_cap=1)
        self.assertTrue(budget.try_acquire("Bob"))
        worker = AgenticCachePlanningWorker(
            agent=SimpleNamespace(username="Alice"),
            teammates=(),
            llm_client=llm,
            prompt_builder=AgenticCachePromptBuilder(),
            codec=AgenticCacheDecisionCodec(
                registry,
                build_agentic_cache_instructions(),
            ),
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(),
            registry=registry,
            spec=SimpleNamespace(description="", completion={}),
            cache=AgenticCacheStore(),
            coordinator=coordinator,
            acting_worker=SimpleNamespace(is_busy=False, seen_version=0),
            budget=budget,
            event_sink=lambda event_type, **data: events.append((event_type, data)),
        )

        worker.iterate()
        worker.iterate()

        self.assertIsNone(buffer.peek())
        self.assertEqual(llm.calls, 0)
        self.assertEqual(worker.cache_miss_count, 1)
        self.assertEqual(
            [event for event, _ in events],
            ["cache.miss", "planning.budget_exhausted"],
        )

    def test_acting_worker_confirms_successful_transition(self) -> None:
        registry = SkillRegistry(allowed_actions={"inventory.list"})
        memory = TeamMemory()
        buffer = SingleSlotOverwriteBuffer()
        cache = AgenticCacheStore()
        coordinator = AgenticCacheCoordinator()
        agent = SimpleNamespace(
            username="Alice",
            action_planner=_FakePlanner(),
        )
        action_unit = ActionUnit(
            agent_id="Alice",
            action="inventory.list",
            params={},
            mode=ActionMode.ONESHOT,
        )
        instructions = build_agentic_cache_instructions()
        proposal = AgenticCacheProposal(
            action_unit=action_unit,
            previous_action=ActionPlan("START"),
            state_metadata={"action_index": 0},
            reason="list inventory",
        )
        buffer.put(proposal)
        worker = AgenticCacheActingWorker(
            agent=agent,
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(broadcast_results=False),
            cache=cache,
            coordinator=coordinator,
            instructions=instructions,
            task_category="monster_clearance",
        )

        worker.iterate()

        entries = cache.list_entries(owner_agent_id="Alice")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].previous_action.name, "START")
        self.assertEqual(entries[0].next_action.name, "inventory")
        self.assertEqual(coordinator.previous_action().name, "inventory")

    def test_dead_acting_worker_skips_execution_and_cache_update(self) -> None:
        class CountingPlanner(_FakePlanner):
            def __init__(self) -> None:
                self.calls = 0

            def execute_unit(self, unit):
                self.calls += 1
                return super().execute_unit(unit)

        memory = TeamMemory()
        buffer = SingleSlotOverwriteBuffer()
        cache = AgenticCacheStore()
        coordinator = AgenticCacheCoordinator()
        planner = CountingPlanner()
        agent = SimpleNamespace(username="Alice", action_planner=planner)
        gate = EntityLivenessGate([agent])
        gate.mark_live("Alice", False, reason="test")
        proposal = AgenticCacheProposal(
            action_unit=ActionUnit(
                agent_id="Alice",
                action="inventory.list",
                params={},
                mode=ActionMode.ONESHOT,
            ),
            previous_action=ActionPlan("START"),
            state_metadata={"action_index": 0},
            reason="list inventory",
        )
        buffer.put(proposal)
        worker = AgenticCacheActingWorker(
            agent=agent,
            buffer=buffer,
            memory=memory,
            config=AgenticCacheConfig(broadcast_results=False),
            cache=cache,
            coordinator=coordinator,
            instructions=build_agentic_cache_instructions(),
            liveness_gate=gate,
        )

        worker.iterate()

        self.assertEqual(planner.calls, 0)
        self.assertEqual(cache.list_entries(owner_agent_id="Alice"), ())
        self.assertEqual(coordinator.previous_action().name, "START")


class AgenticCacheRunnerSystemMessageTests(unittest.TestCase):
    def test_config_validates_system_message_fields(self) -> None:
        for kwargs in (
            {"system_messages_enabled": "yes"},
            {"system_message_interval": 0},
            {"system_message_interval": -1},
            {"system_message_interval": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AgenticCacheConfig(**kwargs)

    def test_enabled_system_message_is_available_to_prompt_context(self) -> None:
        class OneShotManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "system note" if self.calls == 1 else None

        class FakeSession:
            def __init__(self) -> None:
                self.spec = SimpleNamespace(task_id="task.1", category="debug")
                self.agents = []
                self.memory = TeamMemory()
                self.commands: list[str] = []
                self.server = SimpleNamespace(send_command=self.commands.append)
                self.manager = OneShotManager()
                self.context = SimpleNamespace(elapsed_seconds=lambda: 0.0)
                self.closed = False

            def close(self):
                self.closed = True
                return []

        session = FakeSession()
        runner = AgenticCacheRunner(
            session=session,
            llm_client=object(),
            config=AgenticCacheConfig(
                system_messages_enabled=True,
                system_message_interval=0.01,
            ),
        )

        try:
            runner._start_system_message_worker()
            deadline = time.monotonic() + 1.0
            while session.manager.calls < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            runner.close()

        self.assertGreaterEqual(session.manager.calls, 1)
        self.assertIn("say system note", session.commands)
        self.assertIn(
            {"source": "system", "content": {"message": "system note"}},
            [{"source": item["source"], "content": item["content"]} for item in runner.memory.prompt_history("Alice", team_limit=5, system_limit=5)],
        )


class AgenticCacheDemoSequentialTests(unittest.TestCase):
    def test_cli_sequential_uses_fresh_llm_client_factory(self) -> None:
        import script.demo_agentic_cache as demo

        clients = []

        class StrictClient:
            def __init__(self, config) -> None:
                self.config = config
                clients.append(self)

        class FakeSequential:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

            def run(self):
                self.kwargs["llm_client_factory"]()
                self.kwargs["llm_client_factory"]()
                return SimpleNamespace(
                    exit_code=0,
                    to_dict=lambda: {"exit_code": 0},
                )

        specs = [
            SimpleNamespace(task_id="monster_clearance", output={}),
            SimpleNamespace(task_id="monster_clearance", output={}),
        ]
        args = SimpleNamespace(
            episode_count=None,
            start_episode=1,
            record_dir=None,
            env_file=Path(".env"),
            run_id=None,
            server_dir=Path("server"),
            bridge_port_start=3201,
            unsafe_actions=False,
            max_run_seconds=30,
            max_llm_calls=8,
            max_llm_calls_per_agent=None,
            no_broadcast_results=False,
            enable_chat_actions=False,
            cache_top_k=5,
            metadata_tolerance=0.0,
            cache_source=None,
        )

        with tempfile.TemporaryDirectory() as tmp:
            args.record_dir = Path(tmp)
            with patch.object(
                demo.LLMConfig,
                "from_env",
                return_value=LLMConfig(
                    api_url="http://example.invalid/v1",
                    api_key="test-key",
                    model="fake-model",
                ),
            ), patch.object(demo, "AgentLLMClientPool", StrictClient), patch.object(
                demo, "AgenticCacheSequentialRunner", FakeSequential
            ):
                exit_code = demo._run_sequential(
                    args,
                    specs,
                    registry=SimpleNamespace(),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])


class _FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):  # pragma: no cover - should not be called
        self.calls += 1
        raise AssertionError("LLM should not be called on cache hit")


class _StaticLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return SimpleNamespace(content=self.content, request_id="req-1")


class _SequenceLLM:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return SimpleNamespace(
            content=self.contents.pop(0),
            request_id=f"req-{self.calls}",
        )


class _FakePlanner:
    registry = SkillRegistry(allowed_actions={"inventory.list"})

    def execute_unit(self, unit):
        return ActionResult(
            action=unit.action,
            ok=True,
            status=ActionStatus.COMPLETED,
            result={"ok": True},
        )


if __name__ == "__main__":
    unittest.main()
