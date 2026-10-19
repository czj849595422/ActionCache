"""COAP 新增模块与 worker/runner 离线测试。"""

from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import (  # noqa: E402
    ActionMode,
    SimpleInstructionRegistry,
    SkillRegistry,
)
from src.baseline.COAP import (  # noqa: E402
    Action,
    ActionSpec,
    BufferUpdateConfig,
    CacheActionCandidatePolicy,
    CacheBootConfig,
    CacheBootMode,
    CacheEntryChange,
    CacheEntry,
    CacheContentResult,
    CacheService,
    CacheSharingMode,
    CandidateRecord,
    COAPCodecError,
    COAPConfig,
    COAPDecisionCodec,
    COAPDecisionCoordinator,
    COAPRunner,
    SystemClock,
    CandidateStatus,
    ConsistencyError,
    ImportantParameterActionConsistency,
    InMemoryCacheRepository,
    load_shared_cache_snapshot,
    RetrievalConfig,
    ScoreConfig,
    ScoreStrategy,
    ScoreUpdater,
)
from src.baseline.COAP.adapters import (  # noqa: E402
    action_unit_to_action,
    broadcast_text,
    render_catalog,
)
from src.baseline.COAP.schemas import (  # noqa: E402
    COAPProposal,
    COAPRunResult,
    ScoreUpdateReport,
)
from src.baseline.COAP.workers import (  # noqa: E402
    COAPActingWorker,
    COAPCachePlanningWorker,
    COAPCacheWorker,
    COAPPlanningWorker,
)
from src.interface import ActionUnit  # noqa: E402
from src.llm import StreamEventType  # noqa: E402
from src.memory import TeamMemory, render_history  # noqa: E402
from src.recording.table import ActionExecutionTable, make_call_file_resolver  # noqa: E402
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer  # noqa: E402
from src.tasks import TaskOutcome, TaskStatus, task_action_space  # noqa: E402


KEYWORD = "</action>"


def mk_action(name: str, **params) -> Action:
    return Action(name=name, parameters=params or {})


def mk_entry(
    previous: Action,
    next_action: Action,
    *,
    score: float = 0.5,
    task_scope: str | None = None,
) -> CacheEntry:
    return CacheEntry(
        previous_action=previous,
        next_action=next_action,
        reason="reason",
        score=score,
        task_scope=task_scope,
    )


def make_cache_service(
    *,
    boot_mode: CacheBootMode = CacheBootMode.EMPTY,
    source_path: str | None = None,
    content_generator=None,
) -> CacheService:
    return CacheService(
        InMemoryCacheRepository(),
        boot_config=CacheBootConfig(mode=boot_mode, source_path=source_path),
        score_config=ScoreConfig(minimum_llm_initial_score=0.05),
        sharing_mode=CacheSharingMode.SHARED,
        content_generator=content_generator,
    )


def all_action_specs() -> list[ActionSpec]:
    registry = SkillRegistry()
    specs: list[ActionSpec] = []
    for skill_name, manifest in sorted(registry.manifests.items()):
        for command_name, command_spec in sorted(manifest.commands.items()):
            specs.append(
                ActionSpec(
                    name=f"{skill_name}.{command_name}",
                    parameter_schema=command_spec.raw.get("params_schema") or {},
                )
            )
    return specs


class FakeContentGenerator:
    def generate(
        self,
        previous_action,
        next_action,
        reasons,
        *,
        existing_reason=None,
        require_initial_score=False,
        owner_agent_id=None,
    ):
        return CacheContentResult(
            reason="抽象原因",
            initial_score=0.5 if require_initial_score else None,
        )


class FakeClock:
    def __init__(
        self,
        now: float | None = None,
        mono: float = 0.0,
    ) -> None:
        self._now = now if now is not None else time.time() + 10.0
        self._mono = mono

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds


class FakeLLM:
    """支持 stream（模拟 KEYWORD_REACHED + COMPLETED）与 complete 的假客户端。"""

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        delay_seconds: float = 0.0,
        triggered_at: float | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list = []
        self.delay_seconds = delay_seconds
        self.triggered_at = triggered_at
        self.complete_calls = 0
        self.stream_calls = 0
        self.tail_gate: threading.Event | None = None
        self.tail_started = threading.Event()
        self.tail_finished = threading.Event()

    def _next(self) -> str:
        text = self.responses.pop(0) if self.responses else self._fallback()
        self.calls.append(None)  # 占位，实际 request 由 complete/stream 记录
        return text

    def _fallback(self) -> str:
        return (
            "<reason>fallback</reason><action>wait(seconds=1)</action>"
        )

    def complete(self, request):
        self.complete_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        text = self._next()
        self.calls[-1] = request
        return SimpleNamespace(
            content=text,
            request_id=f"req-{len(self.calls)}",
            model="fake",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    def stream(self, request, *, triggers=(), stop_event=None):
        self.stream_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        text = self._next()
        self.calls[-1] = request
        self.tail_started.clear()
        self.tail_finished.clear()
        keyword = KEYWORD
        trigger_event = None
        try:
            if keyword in text:
                head = text.split(keyword, 1)[0]
                trigger_event = SimpleNamespace(
                    type=StreamEventType.KEYWORD_REACHED,
                    request_id=f"req-{len(self.calls)}",
                    trigger=SimpleNamespace(
                        segment=head,
                        triggered_at=(
                            self.triggered_at
                            if self.triggered_at is not None
                            else time.time()
                        ),
                    ),
                    response=None,
                )
                yield trigger_event
                self.tail_started.set()
                if self.tail_gate is not None:
                    self.tail_gate.wait(timeout=5.0)
            yield SimpleNamespace(
                type=StreamEventType.COMPLETED,
                request_id=f"req-{len(self.calls)}",
                trigger=None,
                response=SimpleNamespace(content=text),
            )
        finally:
            self.tail_finished.set()


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self.executed: list[ActionUnit] = []
        self.continuous_started: list[ActionUnit] = []

    def execute_unit(self, unit: ActionUnit):
        self.executed.append(unit)
        return SimpleNamespace(
            ok=True,
            status="completed",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "completed", "description": "ok"},
            raw={"ok": True},
        )

    def start_unit(self, unit: ActionUnit):
        self.continuous_started.append(unit)
        return SimpleNamespace(
            ok=True,
            status="running",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "running"},
            raw={"ok": True},
        )

    def status(self, action_id: str):
        return SimpleNamespace(
            ok=True,
            status="completed",
            result={"ok": True, "status": "completed"},
            raw={"ok": True},
        )

    def stop(
        self,
        action_id: str,
        reason: str = "stop",
        timeout: float | None = None,
    ):
        return SimpleNamespace(
            ok=True,
            status="interrupted",
            result={"ok": True, "status": "interrupted"},
            raw={"ok": True},
        )


class FailingStatusPlanner(FakePlanner):
    def status(self, action_id: str):
        return SimpleNamespace(
            ok=False,
            status="failed",
            error="failed after start",
            result={
                "ok": False,
                "status": "failed",
                "error": "failed after start",
            },
            raw={"ok": False},
        )


class FailingStatusPollPlanner(FakePlanner):
    def status(self, action_id: str):
        raise RuntimeError("bridge status unavailable")


class FakeAgent:
    def __init__(self, username: str, registry: SkillRegistry) -> None:
        self.username = username
        self.action_planner = FakePlanner(registry)


def mining_registry() -> SkillRegistry:
    from src.tasks import create_default_registry

    spec = create_default_registry().get("mining")
    return SkillRegistry(allowed_actions=task_action_space(spec))


def build_workers(
    *,
    agent: FakeAgent | None = None,
    responses: list[str] | None = None,
    config: COAPConfig | None = None,
    clock: FakeClock | None = None,
    execution_table: ActionExecutionTable | None = None,
    call_file_resolver=None,
    task_category: str | None = None,
    event_sink=None,
    change_sink=None,
    liveness_gate: EntityLivenessGate | None = None,
):
    """构造一套可手动驱动的 worker 组合（共享 buffer/memory/cache）。"""
    agent = agent or FakeAgent("Alice", mining_registry())
    config = config or COAPConfig(planning_interval=5.0)
    clock = clock or SystemClock()
    memory = TeamMemory()
    cache_service = make_cache_service(content_generator=FakeContentGenerator())
    score_updater = ScoreUpdater(
        cache_service.repository,
        config.cache_score,
        clock=clock,
    )
    cache_service.score_updater = score_updater
    consistency = ImportantParameterActionConsistency(
        config.important_parameters,
        cache_service.action_canonicalizer,
    )
    instructions = SimpleInstructionRegistry()
    buffer = SingleSlotOverwriteBuffer()
    interrupt_event = threading.Event()
    coordinator = COAPDecisionCoordinator(
        agent_id=agent.username,
        cache_service=cache_service,
        task_scope="task.1",
        clock=clock,
    )
    codec = COAPDecisionCodec(
        agent.action_planner.registry,
        instructions,
        enable_chat_actions=config.enable_chat_actions,
        action_ready_keyword=config.action_ready_keyword,
        candidate_policy=config.candidate_policy,
    )
    acting = COAPActingWorker(
        agent=agent,
        buffer=buffer,
        interrupt_event=interrupt_event,
        memory=memory,
        config=config,
        coordinator=coordinator,
        cache_service=cache_service,
        score_updater=score_updater,
        consistency=consistency,
        instructions=instructions,
        execution_table=execution_table,
        call_file_resolver=call_file_resolver,
        task_category=task_category,
        liveness_gate=liveness_gate,
    )
    planning = COAPPlanningWorker(
        agent=agent,
        teammates=(),
        llm_client=FakeLLM(responses),
        prompt_builder=_prompt_builder(config),
        codec=codec,
        buffer=buffer,
        interrupt_event=interrupt_event,
        memory=memory,
        config=config,
        coordinator=coordinator,
        registry=agent.action_planner.registry,
        spec=_fake_spec(),
        instructions=instructions,
        event_sink=event_sink,
        clock=clock,
        liveness_gate=liveness_gate,
    )
    cache_planning = COAPCachePlanningWorker(
        agent=agent,
        llm_client=FakeLLM(),
        prompt_builder=_prompt_builder(config),
        codec=codec,
        buffer=buffer,
        interrupt_event=interrupt_event,
        memory=memory,
        config=config,
        coordinator=coordinator,
        registry=agent.action_planner.registry,
        spec=_fake_spec(),
        instructions=instructions,
        event_sink=event_sink,
        clock=clock,
        liveness_gate=liveness_gate,
    )
    cache_worker = COAPCacheWorker(
        cache_service=cache_service,
        score_updater=score_updater,
        task_scope="task.1",
        config=config,
        event_sink=event_sink,
        change_sink=change_sink,
        liveness_gate=liveness_gate,
    )
    cache_worker._agent_ids_value = (agent.username,)
    return SimpleNamespace(
        agent=agent,
        memory=memory,
        cache_service=cache_service,
        score_updater=score_updater,
        consistency=consistency,
        buffer=buffer,
        interrupt_event=interrupt_event,
        coordinator=coordinator,
        codec=codec,
        acting=acting,
        planning=planning,
        cache_planning=cache_planning,
        cache_worker=cache_worker,
        clock=clock,
    )


def _prompt_builder(config: COAPConfig):
    from src.baseline.COAP import COAPPromptBuilder

    return COAPPromptBuilder(
        language=config.language,
        action_ready_keyword=config.action_ready_keyword,
        candidate_policy=config.candidate_policy,
    )


def _fake_spec():
    return SimpleNamespace(
        task_id="task.1",
        description="测试任务",
        category="mining",
        load_description_text=lambda: "测试任务描述",
    )


class ConsistencyTests(unittest.TestCase):
    def test_default_config_sets_documented_important_parameters(self) -> None:
        config = COAPConfig()

        self.assertEqual(
            config.important_parameters["combat.fight"],
            ("target",),
        )
        self.assertEqual(
            config.important_parameters["crafting.craft_item"],
            ("item",),
        )
        self.assertEqual(
            config.important_parameters["furnace.smelt_item"],
            ("position", "item"),
        )
        self.assertEqual(
            config.important_parameters["equipment.equip_items"],
            ("items[].name",),
        )
        self.assertEqual(
            config.important_parameters["inventory.drop"],
            ("items[].name",),
        )
        self.assertEqual(
            config.important_parameters["inventory.give"],
            ("target", "items[].name"),
        )
        self.assertNotIn("count", config.important_parameters["crafting.craft_item"])
        self.assertNotIn("count", config.important_parameters["furnace.put_input"])
        self.assertNotIn("count", config.important_parameters["trade.buy_from_shop"])
        self.assertEqual(config.important_parameters["chat.say"], ())
        self.assertEqual(config.important_parameters["furnace.put_fuel"], ())
        self.assertEqual(config.important_parameters["wait.wait"], ())
        self.assertNotIn("weapon", config.important_parameters["combat.fight"])
        self.assertNotIn("fuel", config.important_parameters["furnace.smelt_item"])

    def test_default_important_parameters_validate_current_manifests(self) -> None:
        config = COAPConfig()
        action_space = all_action_specs()
        consistency = ImportantParameterActionConsistency(
            config.important_parameters,
            make_cache_service().action_canonicalizer,
            require_explicit_actions=True,
        )

        consistency.validate_action_space(action_space)
        self.assertEqual(
            set(config.important_parameters),
            {spec.name for spec in action_space},
        )

    def test_important_item_selector_ignores_item_counts(self) -> None:
        canonicalizer = make_cache_service().action_canonicalizer
        consistency = ImportantParameterActionConsistency(
            {"inventory.drop": ("items[].name",)},
            canonicalizer,
        )

        left = mk_action(
            "inventory.drop",
            items=[{"name": "cobblestone", "count": 1}],
        )
        right = mk_action(
            "inventory.drop",
            items=[{"name": "cobblestone", "count": 64}],
        )
        different = mk_action(
            "inventory.drop",
            items=[{"name": "dirt", "count": 1}],
        )

        self.assertTrue(consistency.equivalent(left, right))
        self.assertFalse(consistency.equivalent(left, different))

    def test_important_position_and_combat_target_selectors_normalize_inputs(
        self,
    ) -> None:
        canonicalizer = make_cache_service().action_canonicalizer
        consistency = ImportantParameterActionConsistency(
            {
                "mining.mine_iron": ("position",),
                "combat.attack": ("target",),
            },
            canonicalizer,
        )

        self.assertTrue(
            consistency.equivalent(
                mk_action("mining.mine_iron", position={"x": 1, "y": 2, "z": 3}),
                mk_action("mining.mine_iron", x=1, y=2, z=3),
            )
        )
        self.assertTrue(
            consistency.equivalent(
                mk_action("combat.attack", target="zombie_73"),
                mk_action(
                    "combat.attack",
                    target={"name": "zombie", "entity_id": 73},
                ),
            )
        )

    def test_equivalent_compares_important_parameters(self) -> None:
        canonicalizer = make_cache_service().action_canonicalizer
        consistency = ImportantParameterActionConsistency(
            {"navigation.goto_position": ("x", "y", "z")},
            canonicalizer,
        )
        left = mk_action("navigation.goto_position", x=1, y=2, z=3)
        right = mk_action("navigation.goto_position", x=1, y=2, z=3)
        self.assertTrue(consistency.equivalent(left, right))
        different = mk_action("navigation.goto_position", x=9, y=2, z=3)
        self.assertFalse(consistency.equivalent(left, different))
        other_name = mk_action("navigation.follow_position", x=1, y=2, z=3)
        self.assertFalse(consistency.equivalent(left, other_name))

    def test_unknown_important_parameter_rejected(self) -> None:
        consistency = ImportantParameterActionConsistency(
            {"wait.wait": ("nonexistent",)},
            make_cache_service().action_canonicalizer,
        )
        from src.baseline.COAP import ActionSpec

        with self.assertRaises(ConsistencyError):
            consistency.validate_action_space(
                [ActionSpec(name="wait.wait")]
            )


class ScoreUpdaterTests(unittest.TestCase):
    def test_episode_statistics_are_local_while_entry_counts_accumulate(self) -> None:
        clock = FakeClock()
        repo = InMemoryCacheRepository()
        entry = CacheEntry(
            previous_action=mk_action("wait"),
            next_action=mk_action("goto"),
            reason="reason",
            hit_count=10,
            attempt_count=10,
            success_count=9,
        )
        repo.upsert(entry)
        updater = ScoreUpdater(repo, ScoreConfig(), clock=clock)

        updater.record_hits([entry.entry_id])
        for index in range(5):
            updater.record_feedback(
                entry.entry_id,
                feedback_id=f"episode-2-{index}",
                success=index < 4,
            )

        self.assertEqual(
            updater.episode_statistics(),
            {"hit_count": 1, "attempt_count": 5, "success_count": 4},
        )
        current = repo.get(entry.entry_id)
        self.assertEqual(current.hit_count, 11)
        self.assertEqual(current.attempt_count, 15)
        self.assertEqual(current.success_count, 13)

        updater.update_scores()
        self.assertEqual(
            updater.episode_statistics(),
            {"hit_count": 1, "attempt_count": 5, "success_count": 4},
        )

    def test_confidence_adjusted_update(self) -> None:
        clock = FakeClock()
        repo = InMemoryCacheRepository()
        entry = mk_entry(mk_action("wait"), mk_action("goto"), score=0.5)
        repo.upsert(entry)
        updater = ScoreUpdater(
            repo,
            ScoreConfig(
                strategy=ScoreStrategy.CONFIDENCE_ADJUSTED,
                lambda_value=5.0,
                minimum_feedback_count=2,
            ),
            clock=clock,
        )
        updater.record_feedback(entry.entry_id, feedback_id="f1", success=True)
        updater.record_feedback(entry.entry_id, feedback_id="f2", success=True)
        self.assertTrue(updater.should_update())
        report = updater.update_scores()
        updated = repo.get(entry.entry_id)
        self.assertIsNotNone(updated)
        self.assertGreater(updated.score, 0.5)
        self.assertIn(entry.entry_id, report.updated_entry_ids)
        self.assertEqual(updated.delta_attempt_count, 0)
        self.assertEqual(len(report.changes), 1)
        self.assertEqual(report.changes[0].event, "score_update")
        self.assertEqual(report.changes[0].before.score, 0.5)
        self.assertEqual(report.changes[0].after.entry_id, entry.entry_id)

    def test_realtime_update_triggers_on_single_feedback(self) -> None:
        clock = FakeClock()
        repo = InMemoryCacheRepository()
        entry = mk_entry(mk_action("wait"), mk_action("goto"), score=0.5)
        repo.upsert(entry)
        updater = ScoreUpdater(repo, ScoreConfig(), clock=clock)
        self.assertFalse(updater.should_update())
        updater.record_feedback(entry.entry_id, feedback_id="f1", success=True)
        self.assertTrue(updater.should_update())
        report = updater.update_scores()
        self.assertIn(entry.entry_id, report.updated_entry_ids)

    def test_default_bayesian_update_uses_cumulative_counts(self) -> None:
        clock = FakeClock()
        repo = InMemoryCacheRepository()
        entry = mk_entry(mk_action("wait"), mk_action("goto"), score=0.5)
        repo.upsert(entry)
        updater = ScoreUpdater(repo, ScoreConfig(lambda_value=5.0), clock=clock)

        updater.record_feedback(entry.entry_id, feedback_id="f1", success=True)
        updater.update_scores()
        updated = repo.get(entry.entry_id)
        self.assertAlmostEqual(updated.score, 3.5 / 6.0, places=6)

        updater.record_feedback(entry.entry_id, feedback_id="f2", success=False)
        updater.update_scores()
        updated = repo.get(entry.entry_id)
        self.assertAlmostEqual(updated.score, 3.5 / 7.0, places=6)

    def test_frequency_aware_idle_decay(self) -> None:
        clock = FakeClock()
        repo = InMemoryCacheRepository()
        entry = mk_entry(mk_action("wait"), mk_action("goto"), score=0.8)
        repo.upsert(entry)
        updater = ScoreUpdater(
            repo,
            ScoreConfig(strategy=ScoreStrategy.FREQUENCY_AWARE, idle_decay=0.1),
            clock=clock,
        )
        clock.advance(100.0)
        updater.update_scores()
        updated = repo.get(entry.entry_id)
        self.assertAlmostEqual(updated.score, 0.72, places=6)


class CodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = mining_registry()
        self.instructions = SimpleInstructionRegistry()
        self.codec = COAPDecisionCodec(
            self.registry,
            self.instructions,
            enable_chat_actions=False,
            action_ready_keyword=KEYWORD,
        )

    def test_standard_parse_with_interrupt(self) -> None:
        text = (
            "<reason>紧急</reason><interrupt>true</interrupt>"
            "<action>wait(seconds=5)</action>"
        )
        proposal = self.codec.parse_complete(
            text,
            agent_id="Alice",
            operation="timed_standard_action",
        )
        self.assertTrue(proposal.interrupt)
        self.assertEqual(proposal.path, "standard")
        self.assertEqual(proposal.action_unit.action, "wait.wait")

    def test_shorthand_alias_expansion(self) -> None:
        text = (
            "<reason>r</reason><action>wait(seconds=1)</action>"
        )
        proposal = self.codec.parse_complete(
            text,
            agent_id="Alice",
            operation="standard_action",
        )
        self.assertEqual(proposal.action_unit.action, "wait.wait")
        self.assertEqual(proposal.action_unit.params, {"seconds": 1})

    def test_goto_position_formats_parse_complete_and_streamed(self) -> None:
        cases = (
            ("goto(0,0,0)", {"x": 0, "y": 0, "z": 0}),
            ("goto((0,0,0))", {"x": 0, "y": 0, "z": 0}),
            ("goto((-1.5,0.0,-11.5))", {"x": -1.5, "y": 0.0, "z": -11.5}),
            ("goto({x:-45,y:0,z:-25})", {"x": -45, "y": 0, "z": -25}),
            ("goto(position={'x':1,'y':0,'z':2})", {"x": 1, "y": 0, "z": 2}),
        )
        for call, expected in cases:
            with self.subTest(call=call, mode="complete"):
                proposal = self.codec.parse_complete(
                    f"<reason>r</reason><action>{call}</action>",
                    agent_id="Alice",
                    operation="standard_action",
                )
                self.assertEqual(proposal.action_unit.action, "navigation.goto_position")
                self.assertEqual(proposal.action_unit.mode, "continuous")
                self.assertEqual(proposal.action_unit.params, expected)
            with self.subTest(call=call, mode="streamed"):
                proposal = self.codec.parse_streamed_action(
                    f"<action>{call}",
                    agent_id="Alice",
                    operation="cache_action",
                )
                self.assertEqual(proposal.action_unit.action, "navigation.goto_position")
                self.assertEqual(proposal.action_unit.mode, "continuous")
                self.assertEqual(proposal.action_unit.params, expected)

    def test_chat_action_rejected(self) -> None:
        text = (
            "<reason>r</reason><action>chat(message='hi')</action>"
        )
        with self.assertRaises(COAPCodecError):
            self.codec.parse_complete(
                text,
                agent_id="Alice",
                operation="standard_action",
            )

    def test_cache_path_parse(self) -> None:
        text = "<action>wait(seconds=1)</action>"
        proposal = self.codec.parse_complete(
            text,
            agent_id="Alice",
            operation="cache_action",
            cache_candidates=[],
        )
        self.assertEqual(proposal.path, "cache")
        self.assertFalse(proposal.interrupt)

    def test_cache_path_allows_action_outside_candidates(self) -> None:
        strict_codec = COAPDecisionCodec(
            self.registry,
            self.instructions,
            candidate_policy=CacheActionCandidatePolicy.STRICT,
        )
        proposal = strict_codec.parse_complete(
            "<action>wait(seconds=1)</action>",
            agent_id="Alice",
            operation="cache_action",
            cache_candidates=[
                mk_entry(mk_action("wait"), mk_action("goto")),
            ],
        )

        self.assertEqual(proposal.action_unit.action, "wait.wait")


class AdapterTests(unittest.TestCase):
    def test_render_catalog_filters_chat(self) -> None:
        registry = mining_registry()
        instructions = SimpleInstructionRegistry()
        catalog = render_catalog(
            registry,
            instructions,
            "zh",
            enable_chat_actions=False,
        )
        self.assertNotIn("chat(", catalog)
        with_chat = render_catalog(
            registry,
            instructions,
            "zh",
            enable_chat_actions=True,
        )
        self.assertIn("chat(", with_chat)

    def test_broadcast_text(self) -> None:
        unit = ActionUnit(
            agent_id="Alice",
            action="wait.wait",
            params={"seconds": 1},
            mode="continuous",
        )
        proposal = COAPProposal(
            action_unit=unit,
            path="standard",
            input_params={"seconds": 1},
        )
        instructions = SimpleInstructionRegistry()
        text = broadcast_text("Alice", proposal, "completed", None, instructions)
        self.assertIn("执行wait(", text)
        self.assertIn("完成", text)


class CoordinatorTests(unittest.TestCase):
    def test_race_claim_only_one_winner(self) -> None:
        cache = make_cache_service()
        coordinator = COAPDecisionCoordinator(
            agent_id="Alice",
            cache_service=cache,
            task_scope="t",
        )
        coordinator.begin_race()
        self.assertTrue(coordinator.try_claim())
        self.assertFalse(coordinator.try_claim())
        coordinator.on_action_started()
        self.assertTrue(coordinator.try_claim())


class ActingWorkerTests(unittest.TestCase):
    def test_clearance_fight_defaults_collect_none(self) -> None:
        env = build_workers(task_category="monster_clearance")
        proposal = COAPProposal(
            action_unit=ActionUnit(
                agent_id="Alice",
                action="combat.fight",
                params={"collect_drops": "optional"},
                mode="continuous",
            ),
            input_params={},
        )
        env.buffer.put(proposal)
        env.acting.iterate()
        unit = env.agent.action_planner.continuous_started[0]
        self.assertEqual(unit.params["collect_drops"], "none")

        plain = build_workers()
        plain.buffer.put(
            COAPProposal(
                action_unit=ActionUnit(
                    agent_id="Alice",
                    action="combat.fight",
                    params={"collect_drops": "optional"},
                    mode="continuous",
                ),
                input_params={},
            )
        )
        plain.acting.iterate()
        self.assertEqual(
            plain.agent.action_planner.continuous_started[0].params[
                "collect_drops"
            ],
            "optional",
        )

    def test_execute_and_record_candidate(self) -> None:
        env = build_workers()
        proposal = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=1)</action>",
            agent_id="Alice",
            operation="standard_action",
        )
        entry = env.memory.append_history_event(
            agent_id="Alice",
            source="self",
            event_type="planning",
            content={"standard_plan": "wait.wait", "selected_action": "wait.wait"},
        )
        proposal.memory_entry_id = entry.entry_id
        env.buffer.put(proposal)
        env.acting.iterate()
        env.acting.iterate()
        self.assertEqual(
            env.agent.action_planner.continuous_started[0].action,
            "wait.wait",
        )
        self.assertEqual(env.coordinator.last_action.name, "wait.wait")
        records = env.cache_service.buffer.snapshot()
        self.assertEqual(records, ())
        chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertTrue(any(item["content"].get("result_description") for item in chats))

    def test_failed_executed_action_commits_communication(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        agent.action_planner = FailingStatusPlanner(agent.action_planner.registry)
        env = build_workers(agent=agent)
        proposal = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=1)</action><chat>失败后重选路线</chat>",
            agent_id="Alice",
            operation="standard_action",
        )
        env.buffer.put(proposal)

        env.acting.iterate()
        env.acting.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertTrue(
            any("失败后重选路线" in str(item["content"]) for item in active_chats)
        )

    def test_status_failures_release_active_action(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        agent.action_planner = FailingStatusPollPlanner(agent.action_planner.registry)
        env = build_workers(agent=agent)
        proposal = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=10)</action>",
            agent_id="Alice",
            operation="standard_action",
        )
        env.buffer.put(proposal)
        env.acting.iterate()

        for _ in range(env.acting.config.max_consecutive_errors):
            env.acting.iterate()

        self.assertIsNone(env.acting.active_proposal)
        self.assertIsNone(env.acting.active_action_id)

    def test_direct_feedback_is_not_counted_again_by_content_flush(self) -> None:
        env = build_workers()
        entry = mk_entry(
            mk_action("goto"),
            mk_action("wait.wait", seconds=1),
            score=0.5,
        )
        entry.task_scope = "task.1"
        env.cache_service.repository.upsert(entry)
        env.coordinator.last_action = mk_action("goto")
        proposal = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=1)</action>",
            agent_id="Alice",
            operation="standard_action",
        )
        env.buffer.put(proposal)
        env.acting.iterate()
        env.acting.iterate()

        records = env.cache_service.buffer.snapshot()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].cache_statistics_applied)
        updated = env.cache_service.repository.get(entry.entry_id)
        self.assertEqual(updated.attempt_count, 1)
        self.assertEqual(updated.success_count, 1)

        aggregated = env.cache_service.aggregator.aggregate(records)
        env.cache_service.content_updater.apply(
            aggregated,
            task_scope="task.1",
            owner_agent_id=None,
        )
        after_flush = env.cache_service.repository.get(entry.entry_id)
        self.assertEqual(after_flush.attempt_count, 1)
        self.assertEqual(after_flush.success_count, 1)

    def test_interrupt_records_transition(self) -> None:
        env = build_workers()
        first = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=30)</action>",
            agent_id="Alice",
            operation="standard_action",
        )
        env.buffer.put(first)
        env.acting.iterate()
        self.assertEqual(len(env.agent.action_planner.continuous_started), 1)
        env.interrupt_event.set()
        env.acting.iterate()
        self.assertIsNone(env.acting.active_proposal)
        self.assertEqual(env.coordinator.last_action.name, "wait.wait")
        records = env.cache_service.buffer.snapshot()
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].status, CandidateStatus.UNCERTAIN)
        self.assertFalse(records[0].cache_statistics_applied)

    def test_interrupted_action_does_not_update_cache_statistics(self) -> None:
        env = build_workers()
        entry = mk_entry(mk_action("wait.wait"), mk_action("wait.wait"), score=0.5)
        entry.task_scope = "task.1"
        env.cache_service.repository.upsert(entry)
        first = env.codec.parse_complete(
            "<reason>r</reason><action>wait(seconds=30)</action>",
            agent_id="Alice",
            operation="standard_action",
        )
        env.buffer.put(first)
        env.acting.iterate()
        env.interrupt_event.set()
        env.acting.iterate()

        updated = env.cache_service.repository.get(entry.entry_id)
        self.assertEqual(updated.attempt_count, 0)
        self.assertEqual(updated.success_count, 0)

    def test_execution_table_row_written(self) -> None:
        import csv

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            table = ActionExecutionTable(Path(tmp) / "actions.csv")
            env = build_workers(execution_table=table)
            proposal = env.codec.parse_complete(
                "<reason>r</reason><action>wait(seconds=1)</action>",
                agent_id="Alice",
                operation="standard_action",
            )
            proposal.planning_started_at = 100.0
            proposal.planning_finished_at = 102.0
            env.buffer.put(proposal)
            env.acting.iterate()
            env.acting.iterate()
            table.close()
            rows = list(
                csv.reader(
                    (Path(tmp) / "actions.csv").open(encoding="utf-8-sig")
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "sequence")
            row = rows[1]
            self.assertEqual(row[1], "Alice")
            self.assertIn("wait.wait", row[2])
            self.assertEqual(row[4], "100.0")
            self.assertEqual(row[5], "102.0")
            self.assertEqual(row[11], "False")
            self.assertIn("状态 completed", row[12])
            self.assertEqual(row[13], "standard")

    def test_interrupt_writes_rows_with_flags(self) -> None:
        import csv

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            table = ActionExecutionTable(Path(tmp) / "actions.csv")
            env = build_workers(execution_table=table)
            first = env.codec.parse_complete(
                "<reason>r</reason><action>wait(seconds=30)</action>",
                agent_id="Alice",
                operation="standard_action",
            )
            env.buffer.put(first)
            env.acting.iterate()
            env.interrupt_event.set()
            env.acting.iterate()
            table.close()
            rows = list(
                csv.reader(
                    (Path(tmp) / "actions.csv").open(encoding="utf-8-sig")
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][10], "False")
            self.assertEqual(rows[1][11], "True")


class PlanningWorkerTests(unittest.TestCase):
    def _wait_for(self, predicate, *workers) -> None:
        deadline = time.time() + 1.0
        while time.time() < deadline:
            for worker in workers:
                worker.iterate()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("condition was not reached before timeout")

    def test_action_planning_prefers_streaming_client(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        self.assertEqual(env.planning.llm_client.stream_calls, 1)
        self.assertEqual(env.planning.llm_client.complete_calls, 0)

    def test_planning_duration_ends_when_action_keyword_is_reached(self) -> None:
        clock = FakeClock(now=1000.0, mono=0.0)
        env = build_workers(clock=clock)
        env.planning.llm_client.triggered_at = 1001.25
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        _, proposal = env.buffer.peek()
        self.assertEqual(proposal.planning_started_at, 1000.0)
        self.assertEqual(proposal.planning_finished_at, 1001.25)

    def test_stream_dispatches_before_terminal_and_discards_late_communication(
        self,
    ) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>"
                "<chat>late communication</chat>"
            ]
        )
        llm = env.planning.llm_client
        tail_gate = threading.Event()
        llm.tail_gate = tail_gate
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        self.assertTrue(llm.tail_started.wait(timeout=1.0))
        _, proposal = env.buffer.peek()
        env.acting.iterate()
        self.assertEqual(len(env.agent.action_planner.continuous_started), 1)
        env.acting.iterate()
        self.assertFalse(llm.tail_finished.is_set())

        tail_gate.set()
        self.assertTrue(llm.tail_finished.wait(timeout=1.0))
        history = env.memory.get(proposal.memory_entry_id)
        self.assertIsNotNone(history)
        self.assertIsNone(history.content.get("communication"))

    def test_slow_race_loser_finishes_as_detached_cache_material(self) -> None:
        env = build_workers(
            responses=[
                "<reason>standard reason</reason><action>wait(seconds=1)</action>"
                "<chat>late standard communication</chat>"
            ]
        )
        env.planning.llm_client.delay_seconds = 0.08
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.last_action = mk_action("wait.wait", seconds=1)
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait", seconds=1),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.coordinator.begin_race()
        env.coordinator.request_cache_race()

        env.planning.iterate()
        env.cache_planning.llm_client.responses = [
            "<action>wait(seconds=1)</action>"
        ]
        env.cache_planning.iterate()
        _, winning = env.buffer.peek()
        self.assertEqual(winning.path, "cache")

        self._wait_for(
            lambda: any(
                item.metadata.get("detached")
                for item in env.memory.snapshot()
                if item.key == "history"
            ),
            env.planning,
        )
        detached = [
            item
            for item in env.memory.snapshot()
            if item.key == "history" and item.metadata.get("detached")
        ]
        self.assertEqual(len(detached), 1)
        self.assertIn(
            "普通规划：wait(seconds=1)",
            render_history(
                [
                    {
                        "source": "self",
                        "agent_id": item.agent_id,
                        "event_type": "planning",
                        "content": item.content,
                        "metadata": item.metadata,
                    }
                    for item in detached
                ]
            ),
        )
        self.assertTrue(
            any(
                record.status is CandidateStatus.UNCERTAIN
                and record.metadata.get("detached")
                for record in env.cache_service.buffer.snapshot()
            )
        )
        self.assertIn("standard reason", detached[0].content["planning_output"])
        self.assertEqual(
            detached[0].content["communication"],
            "late standard communication",
        )
        self.assertEqual(env.buffer.peek()[1].path, "cache")
        self.assertIsNotNone(detached[0].metadata.get("race_comparison"))

    def test_detached_race_request_does_not_block_next_standard_submission(self) -> None:
        env = build_workers(
            responses=[
                "<reason>slow</reason><action>wait(seconds=1)</action>",
                "<reason>next</reason><action>wait(seconds=1)</action>",
            ]
        )
        env.planning.llm_client.delay_seconds = 0.25
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.last_action = mk_action("wait.wait", seconds=1)
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait", seconds=1),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.coordinator.begin_race()
        env.coordinator.request_cache_race()
        env.planning.iterate()

        env.cache_planning.llm_client.responses = [
            "<action>wait(seconds=1)</action>"
        ]
        env.cache_planning.iterate()
        self.assertIsNotNone(env.buffer.peek())
        env.acting.iterate()
        deadline = time.time() + 1.0
        while not env.coordinator.action_finished.is_set() and time.time() < deadline:
            env.acting.iterate()
            time.sleep(0.005)
        self.assertTrue(env.coordinator.action_finished.is_set())
        env.planning.iterate()
        self.assertGreaterEqual(env.planning.llm_client.stream_calls, 2)

    def test_late_cache_race_output_does_not_enter_next_history(self) -> None:
        env = build_workers(
            responses=[
                "<reason>standard</reason><action>wait(seconds=1)</action>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.last_action = mk_action("wait.wait", seconds=1)
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait", seconds=1),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.coordinator.begin_race()
        env.coordinator.request_cache_race()
        env.planning.iterate()

        cache_llm = env.cache_planning.llm_client
        tail_gate = threading.Event()
        cache_llm.tail_gate = tail_gate
        cache_llm.responses = [
            "<action>wait(seconds=1)</action><chat>stale cache communication</chat>"
        ]
        env.cache_planning.iterate()
        env.acting.iterate()

        tail_gate.set()
        self.assertTrue(cache_llm.tail_finished.wait(timeout=1.0))
        rendered = render_history(env.memory.prompt_history("Alice"))
        self.assertNotIn("缓存规划：", rendered)
        self.assertNotIn("stale cache communication", rendered)

    def test_cache_stream_dispatches_before_terminal_event(self) -> None:
        env = build_workers()
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.last_action = mk_action("wait.wait", seconds=1)
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait", seconds=1),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.coordinator.begin_race()
        env.coordinator.request_cache_race()
        cache_llm = env.cache_planning.llm_client
        tail_gate = threading.Event()
        cache_llm.tail_gate = tail_gate
        cache_llm.responses = [
            "<action>wait(seconds=1)</action><chat>late cache communication</chat>"
        ]

        env.cache_planning.iterate()

        self.assertTrue(cache_llm.tail_started.wait(timeout=1.0))
        _, proposal = env.buffer.peek()
        self.assertEqual(proposal.path, "cache")
        env.acting.iterate()
        self.assertEqual(len(env.agent.action_planner.continuous_started), 1)
        env.acting.iterate()
        self.assertFalse(cache_llm.tail_finished.is_set())

        tail_gate.set()
        self.assertTrue(cache_llm.tail_finished.wait(timeout=1.0))
        history = env.memory.get(proposal.memory_entry_id)
        self.assertIsNotNone(history)
        self.assertIsNone(history.content.get("communication"))

    def test_initial_plan_retries_after_parse_failure(self) -> None:
        env = build_workers(
            responses=[
                "not a valid action response",
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        self.assertFalse(env.planning._initial_standard_plan_started)
        self.assertFalse(env.buffer.has_new(0))
        self.assertEqual(len(env.planning.llm_client.calls), 1)

        env.planning.iterate()

        self.assertTrue(env.planning._initial_standard_plan_started)
        self.assertTrue(env.buffer.has_new(0))
        self.assertEqual(len(env.planning.llm_client.calls), 2)

    def test_initial_plan_is_executable_without_interrupt(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        self.assertTrue(env.buffer.has_new(0))
        self.assertTrue(env.coordinator.buffer_has_candidate)
        self.assertEqual(
            env.planning.llm_client.calls[0].source.operation,
            "standard_action",
        )
        self.assertEqual(len(env.memory.prompt_history("Alice")), 1)
        rendered = render_history(env.memory.prompt_history("Alice"))
        self.assertIn("普通规划：wait(seconds=1)", rendered)
        self.assertIn("动作选择：wait(seconds=1)", rendered)
        self.assertNotIn("通信：无", rendered)

    def test_candidate_flag_is_cleared_when_publish_is_consumed_immediately(self) -> None:
        """Publishing must not resurrect a candidate already consumed by acting."""
        for path, publisher in (
            ("standard", lambda env, proposal: env.planning._publish(proposal)),
            ("cache", lambda env, proposal: env.cache_planning._publish(proposal)),
        ):
            env = build_workers()
            env.memory.update_observation(
                "Alice",
                "obs",
                metadata={"observed_at": 0.0, "format": "text"},
            )
            env.coordinator.begin_race()
            proposal = COAPProposal(
                action_unit=ActionUnit(
                    agent_id="Alice",
                    action="equipment.equip_items",
                    params={"items": []},
                    mode=ActionMode.ONESHOT,
                ),
                reasoning="r",
            )
            proposal.path = path
            proposal.race_id = env.coordinator.race_id

            original_put = env.buffer.put

            def put_and_consume(item):
                version = original_put(item)
                env.acting.iterate()
                return version

            env.buffer.put = put_and_consume
            publisher(env, proposal)

            self.assertFalse(env.coordinator.buffer_has_candidate, path)
            self.assertIsNone(env.acting.active_proposal, path)
            env.planning.iterate()
            self.assertTrue(env.coordinator.cache_race_event.is_set(), path)

    def test_timed_stream_interrupt_is_published_before_action_dispatch(self) -> None:
        env = build_workers(
            responses=[
                "<reason>紧急</reason><interrupt>true</interrupt>"
                "<action>wait(seconds=30)</action>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        proposal, feedback = env.planning._run_plan_once(
            operation="timed_standard_action",
            cache_candidates=(),
            feedbacks=(),
        )

        self.assertIsNone(feedback)
        self.assertIsNotNone(proposal)
        self.assertTrue(proposal.interrupt)
        self.assertTrue(proposal.publish_to_buffer)
        self.assertTrue(env.interrupt_event.is_set())
        self.assertIsNotNone(env.buffer.peek())

    def test_disabled_standard_verification_starts_with_cache_planning(self) -> None:
        env = build_workers(
            config=COAPConfig(
                planning_interval=5.0,
                standard_verification_enabled=False,
            )
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("START"),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.cache_planning.llm_client.responses = [
            "<action>wait(seconds=1)</action>"
        ]

        env.planning.iterate()

        self.assertTrue(env.planning._initial_standard_plan_started)
        self.assertTrue(env.coordinator.cache_race_event.is_set())
        self.assertEqual(env.planning.llm_client.calls, [])

        env.cache_planning.iterate()

        self.assertEqual(
            env.cache_planning.llm_client.calls[0].source.operation,
            "cache_action",
        )
        self.assertTrue(env.buffer.has_new(0))
        _, proposal = env.buffer.peek()
        self.assertEqual(proposal.path, "cache")

    def test_disabled_standard_and_cache_planning_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            COAPConfig(
                planning_interval=5.0,
                standard_verification_enabled=False,
                cache_planning_enabled=False,
            )

    def test_cache_miss_is_recorded_without_cache_llm_call(self) -> None:
        events: list[tuple[str, dict]] = []
        env = build_workers(event_sink=lambda event, **data: events.append((event, data)))
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.cache_race_event.set()

        env.cache_planning.iterate()

        self.assertEqual(env.cache_planning.retrieval_count, 1)
        self.assertEqual(env.cache_planning.cache_miss_count, 1)
        self.assertEqual(env.cache_planning.planning_call_count, 0)
        self.assertEqual(env.cache_planning.llm_client.calls, [])
        self.assertTrue(any(event == "cache_retrieval.empty" for event, _ in events))

    def test_cache_candidate_starts_cache_planning(self) -> None:
        events: list[tuple[str, dict]] = []
        env = build_workers(event_sink=lambda event, **data: events.append((event, data)))
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.last_action = mk_action("wait.wait", seconds=1)
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait", seconds=1),
                mk_action("wait.wait", seconds=1),
                task_scope="task.1",
            )
        )
        env.cache_planning.llm_client.responses = [
            "<action>wait(seconds=1)</action>"
        ]
        env.coordinator.cache_race_event.set()

        env.cache_planning.iterate()

        self.assertEqual(env.cache_planning.retrieval_count, 1)
        self.assertEqual(env.cache_planning.planning_call_count, 1)
        self.assertEqual(env.cache_planning.planning_success_count, 1)
        self.assertEqual(
            env.cache_planning.llm_client.calls[0].source.operation,
            "cache_action",
        )
        self.assertEqual(env.cache_planning.llm_client.stream_calls, 1)
        self.assertEqual(env.cache_planning.llm_client.complete_calls, 0)
        self.assertTrue(any(event == "cache_planning.started" for event, _ in events))
        self.assertTrue(any(event == "cache_planning.completed" for event, _ in events))

    def test_executable_plan_chat_enters_memory_before_action_finishes(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action><chat>push east</chat>"
            ],
            config=COAPConfig(
                planning_interval=5.0,
                timed_planning_enabled=False,
            ),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertEqual(
            [item["content"]["communication"] for item in active_chats if item["content"].get("communication")],
            ["push east"],
        )

        env.acting.iterate()
        env.acting.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertEqual(
            [item["content"]["communication"] for item in active_chats if item["content"].get("communication")],
            ["push east"],
        )

    def test_disabled_timed_planning_skips_periodic_tick(self) -> None:
        env = build_workers(
            config=COAPConfig(
                planning_interval=5.0,
                timed_planning_enabled=False,
            )
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning._initial_standard_plan_started = True
        env.planning._next_plan_at = 0.0
        env.planning.iterate()
        self.assertEqual(len(env.planning.llm_client.calls), 0)
        self.assertFalse(env.buffer.has_new(0))

    def test_disabled_timed_planning_runs_initial_standard_plan(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(
                planning_interval=5.0,
                timed_planning_enabled=False,
            ),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        self.assertTrue(env.buffer.has_new(0))
        self.assertEqual(len(env.planning.llm_client.calls), 1)
        self.assertEqual(
            env.planning.llm_client.calls[0].source.operation,
            "standard_action",
        )

    def test_action_finished_triggers_dual_race(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()
        self.assertTrue(env.coordinator.cache_race_event.is_set())
        self.assertTrue(env.buffer.has_new(0))

    def test_disabled_cache_planning_uses_standard_plan_after_action(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(
                planning_interval=5.0,
                cache_planning_enabled=False,
            ),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning._initial_standard_plan_started = True
        env.coordinator.on_action_finished(mk_action("wait.wait"))

        env.planning.iterate()

        self.assertFalse(env.coordinator.cache_race_event.is_set())
        self.assertTrue(env.buffer.has_new(0))
        self.assertEqual(len(env.planning.llm_client.calls), 1)
        self.assertEqual(
            env.planning.llm_client.calls[0].source.operation,
            "standard_action",
        )

    def test_disabled_standard_verification_triggers_cache_only(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(
                planning_interval=5.0,
                standard_verification_enabled=False,
            ),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning._initial_standard_plan_started = True
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()
        self.assertTrue(env.coordinator.cache_race_event.is_set())
        self.assertFalse(env.coordinator.race_active)
        self.assertEqual(len(env.planning.llm_client.calls), 0)

    def test_disabled_standard_verification_falls_back_after_cache_failure(self) -> None:
        events: list[tuple[str, dict]] = []

        def event_sink(event_type, **data):
            events.append((event_type, data))

        env = build_workers(
            responses=[
                "<reason>fallback</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(
                planning_interval=5.0,
                standard_verification_enabled=False,
            ),
            event_sink=event_sink,
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning._initial_standard_plan_started = True
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()

        env.cache_planning.iterate()
        self.assertTrue(env.coordinator.cache_plan_failed.is_set())

        env.planning.iterate()
        self.assertTrue(env.buffer.has_new(0))
        self.assertEqual(len(env.planning.llm_client.calls), 1)
        self.assertEqual(
            env.planning.llm_client.calls[0].source.operation,
            "standard_action",
        )
        self.assertFalse(env.coordinator.cache_plan_failed.is_set())

        env.planning.iterate()
        self.assertEqual(len(env.planning.llm_client.calls), 1)
        self.assertTrue(any(event[0] == "cache_planning.failed" for event in events))

    def test_standard_cache_failure_falls_back_with_feedback(self) -> None:
        events: list[tuple[str, dict]] = []
        env = build_workers(
            responses=[
                "not valid",
                "<reason>fallback</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(
                planning_interval=5.0,
                maximum_replans=0,
            ),
            event_sink=lambda event, **data: events.append((event, data)),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait"),
                mk_action("wait.wait"),
                task_scope="task.1",
            )
        )
        env.planning._initial_standard_plan_started = True
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()
        self.assertTrue(env.coordinator.force_dual_next)

        env.cache_planning.llm_client.responses = ["not valid cache action"]
        env.cache_planning.iterate()
        self.assertTrue(env.coordinator.cache_plan_failed.is_set())

        env.planning.iterate()

        self.assertTrue(env.buffer.has_new(0))
        self.assertFalse(env.coordinator.cache_plan_failed.is_set())
        self.assertEqual(len(env.planning.llm_client.calls), 2)
        fallback_user = env.planning.llm_client.calls[1].messages[1]["content"]
        self.assertIn("【规划反馈】", fallback_user)
        self.assertIn("缓存输出解析失败", fallback_user)
        self.assertTrue(any(event == "cache_planning.failed" for event, _ in events))

    def test_cache_failure_is_injected_after_winning_action_finishes(self) -> None:
        env = build_workers(
            responses=[
                "<reason>first</reason><action>wait(seconds=1)</action>",
                "<reason>next</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(planning_interval=5.0),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.cache_service.repository.upsert(
            mk_entry(
                mk_action("wait.wait"),
                mk_action("wait.wait"),
                task_scope="task.1",
            )
        )
        env.planning._initial_standard_plan_started = True
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()
        self.assertTrue(env.buffer.has_new(0))

        env.cache_planning.llm_client.responses = ["not valid cache action"]
        env.cache_planning.iterate()
        self.assertTrue(env.coordinator.cache_plan_failed.is_set())

        env.acting.iterate()
        env.acting.iterate()
        env.planning.iterate()

        self.assertTrue(env.buffer.has_new(0))
        self.assertFalse(env.coordinator.cache_plan_failed.is_set())
        self.assertEqual(len(env.planning.llm_client.calls), 2)
        next_user = env.planning.llm_client.calls[1].messages[1]["content"]
        self.assertIn("【规划反馈】", next_user)
        self.assertIn("缓存输出解析失败", next_user)


class ReplanTimeoutTests(unittest.TestCase):
    def _finish_action(self, env) -> None:
        env.coordinator.on_action_finished(mk_action("wait.wait"))
        env.planning.iterate()

    def test_replan_loop_injects_feedback_and_publishes(self) -> None:
        env = build_workers(
            responses=[
                "not valid json",
                "still not valid json",
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(planning_interval=5.0, maximum_replans=2),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        self._finish_action(env)
        llm = env.planning.llm_client
        self.assertEqual(len(llm.calls), 3)
        self.assertTrue(env.buffer.has_new(0))
        first_user = llm.calls[0].messages[1]["content"]
        second_user = llm.calls[1].messages[1]["content"]
        third_user = llm.calls[2].messages[1]["content"]
        self.assertNotIn("【规划反馈】", first_user)
        self.assertIn("【规划反馈】", second_user)
        self.assertIn("输出解析失败", second_user)
        self.assertIn("输出解析失败", third_user)
        self.assertIn("【规划反馈】", third_user)

    def test_replan_exhaustion_sets_force_dual_next(self) -> None:
        env = build_workers(
            responses=["not valid json", "still not valid json"],
            config=COAPConfig(planning_interval=5.0, maximum_replans=1),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        self._finish_action(env)
        self.assertFalse(env.coordinator.race_active)
        self.assertTrue(env.coordinator.force_dual_next)
        self.assertFalse(env.buffer.has_new(0))
        self.assertIsNone(env.planning.last_plan_error)

        env.planning.iterate()

        self.assertFalse(env.coordinator.force_dual_next)
        self.assertTrue(env.coordinator.race_active)
        self.assertTrue(env.coordinator.cache_race_event.is_set())
        self.assertTrue(env.buffer.has_new(0))

    def test_force_dual_next_ignores_buffered_candidate(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action>"
            ],
            config=COAPConfig(planning_interval=5.0, maximum_replans=1),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.coordinator.force_dual_next = True
        env.coordinator.buffer_has_candidate = True
        self._finish_action(env)
        self.assertFalse(env.coordinator.force_dual_next)
        self.assertTrue(env.coordinator.cache_race_event.is_set())
        self.assertTrue(env.buffer.has_new(0))

    def test_stream_timeout_does_not_publish(self) -> None:
        env = build_workers(
            config=COAPConfig(
                planning_interval=5.0,
                standard_action_timeout_seconds=0.01,
            ),
        )
        env.planning.llm_client.delay_seconds = 0.05
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        self.assertFalse(env.buffer.has_new(0))
        self.assertFalse(env.coordinator.buffer_has_candidate)

    def test_race_ends_and_timed_plan_can_publish_afterwards(self) -> None:
        env = build_workers(
            responses=[
                "not valid json",
                "<reason>r</reason><action>wait(seconds=1)</action>",
            ],
            config=COAPConfig(planning_interval=5.0, maximum_replans=0),
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        self._finish_action(env)
        self.assertFalse(env.coordinator.race_active)
        self.assertTrue(env.coordinator.force_dual_next)

        env.planning.iterate()

        self.assertTrue(env.buffer.has_new(0))
        self.assertFalse(env.coordinator.force_dual_next)
        self.assertTrue(env.coordinator.race_active)
        self.assertTrue(env.coordinator.cache_race_event.is_set())


class CacheWorkerTests(unittest.TestCase):
    def test_flush_updates_cache(self) -> None:
        env = build_workers()
        prev = mk_action("wait.wait")
        nxt = mk_action("navigation.goto_position")
        for index in range(5):
            env.cache_service.buffer.add(
                _record(env, f"d{index}", prev, nxt)
            )
        env.cache_worker.iterate()
        entries = env.cache_service.repository.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].next_action.name, "navigation.goto_position")
        self.assertEqual(env.cache_service.buffer.size(), 0)

    def test_flush_records_change_summary(self) -> None:
        events: list[tuple[str, dict]] = []
        changes = []

        def event_sink(event_type, **data):
            events.append((event_type, data))

        def change_sink(items):
            changes.extend(items)
            return "cache/changes.jsonl"

        env = build_workers(event_sink=event_sink, change_sink=change_sink)
        prev = mk_action("wait.wait")
        nxt = mk_action("navigation.goto_position")
        for index in range(5):
            env.cache_service.buffer.add(_record(env, f"d{index}", prev, nxt))
        env.cache_worker.iterate()

        self.assertEqual(
            [change.event for change in changes],
            ["content_create", "score_update"],
        )
        self.assertEqual(events[0][0], "cache.updated")
        self.assertEqual(events[0][1]["change_count"], 1)
        self.assertEqual(events[0][1]["changes_file"], "cache/changes.jsonl")
        self.assertEqual(events[-1][0], "cache.score_updated")
        self.assertEqual(events[-1][1]["change_count"], 1)
        self.assertEqual(events[-1][1]["changes_file"], "cache/changes.jsonl")

    def test_score_update_records_change_summary(self) -> None:
        events: list[tuple[str, dict]] = []
        changes = []

        def event_sink(event_type, **data):
            events.append((event_type, data))

        def change_sink(items):
            changes.extend(items)
            return "cache/changes.jsonl"

        env = build_workers(event_sink=event_sink, change_sink=change_sink)
        entry = mk_entry(
            mk_action("wait.wait"),
            mk_action("navigation.goto_position"),
            score=0.5,
        )
        entry.task_scope = "task.1"
        env.cache_service.repository.upsert(entry)
        env.score_updater.record_feedback(
            entry.entry_id,
            feedback_id="f1",
            success=True,
        )
        env.cache_worker.iterate()

        self.assertEqual([change.event for change in changes], ["score_update"])
        self.assertEqual(events[-1][0], "cache.score_updated")
        self.assertEqual(events[-1][1]["updated"], 1)
        self.assertEqual(events[-1][1]["change_count"], 1)
        self.assertEqual(events[-1][1]["changes_file"], "cache/changes.jsonl")

    def test_disabled_score_update_keeps_feedback_without_updating_score(self) -> None:
        env = build_workers(config=COAPConfig(score_update_enabled=False))
        entry = mk_entry(
            mk_action("wait.wait"),
            mk_action("navigation.goto_position"),
            score=0.5,
        )
        entry.task_scope = "task.1"
        env.cache_service.repository.upsert(entry)
        env.score_updater.record_feedback(
            entry.entry_id,
            feedback_id="f1",
            success=True,
        )

        env.cache_worker.iterate()

        updated = env.cache_service.repository.get(entry.entry_id)
        self.assertAlmostEqual(updated.score, 0.5)
        self.assertEqual(updated.attempt_count, 1)
        self.assertEqual(updated.success_count, 1)

    def test_final_existing_statistics_flush_updates_only_existing_entry(self) -> None:
        events: list[tuple[str, dict]] = []
        changes = []

        def event_sink(event_type, **data):
            events.append((event_type, data))

        def change_sink(items):
            changes.extend(items)
            return "cache/changes.jsonl"

        env = build_workers(event_sink=event_sink, change_sink=change_sink)
        existing = mk_entry(
            mk_action("wait.wait"),
            mk_action("navigation.goto_position"),
            score=0.5,
        )
        existing.task_scope = "task.1"
        env.cache_service.repository.upsert(existing)
        env.cache_service.buffer.add(
            _record(
                env,
                "existing",
                mk_action("wait.wait"),
                mk_action("navigation.goto_position"),
            )
        )
        env.cache_service.buffer.add(
            _record(
                env,
                "new",
                mk_action("navigation.goto_position"),
                mk_action("wait.wait"),
            )
        )

        env.cache_worker.flush_pending_existing_statistics()

        updated = env.cache_service.repository.get(existing.entry_id)
        self.assertEqual(updated.attempt_count, 1)
        self.assertEqual(updated.success_count, 1)
        self.assertEqual(updated.delta_attempt_count, 0)
        self.assertAlmostEqual(updated.score, 3.5 / 6.0, places=6)
        self.assertEqual(env.cache_service.scoped_entry_count(task_scope="task.1", agent_id="Alice"), 1)
        self.assertEqual(env.cache_service.buffer.size(), 1)
        self.assertEqual(
            [change.event for change in changes],
            ["statistics_update", "score_update"],
        )
        self.assertEqual(events[0][0], "cache.final_statistics_updated")
        self.assertEqual(events[-1][0], "cache.score_updated")

    def test_dead_agent_candidate_is_dropped_before_cache_update(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        gate = EntityLivenessGate([agent])
        gate.mark_live("Alice", False, reason="test")
        env = build_workers(agent=agent, liveness_gate=gate)
        env.cache_service.buffer.add(
            _record(
                env,
                "dead",
                mk_action("wait.wait"),
                mk_action("navigation.goto_position"),
            )
        )

        env.cache_worker.flush_pending_existing_statistics()

        self.assertEqual(env.cache_service.buffer.size(), 0)
        self.assertEqual(env.cache_service.repository.list_entries(), ())


def _record(env, decision_id: str, prev: Action, nxt: Action):
    from src.baseline.COAP import CandidateRecord

    return CandidateRecord(
        decision_id=decision_id,
        agent_id="Alice",
        previous_action=prev,
        next_action=nxt,
        reasoning="reason",
        status=CandidateStatus.SUCCESS,
    )


class FakePerception:
    def perceive(self) -> dict:
        return {"self": {"health": 20}, "metadata": {"mode": "global"}}


class FakeVerbalizer:
    def render(self, observation) -> str:
        return "测试观测文本"


class FakeSession:
    def __init__(self, spec, agents) -> None:
        self.spec = spec
        self.agents = agents
        self.memory = TeamMemory()
        self.server = SimpleNamespace(send_command=lambda command: "ok")
        self.started = False
        self.closed = False
        self.context = None

    def start(self):
        self.started = True

    def poll(self):
        return SimpleNamespace(
            outcome=TaskOutcome(TaskStatus.RUNNING),
            metrics={},
        )

    def close(self):
        self.closed = True
        return []


class RunnerTests(unittest.TestCase):
    def test_worker_cleanup_uses_independent_timeouts(self) -> None:
        class StuckWorker:
            def __init__(self, name: str) -> None:
                self.name = name
                self.join_timeouts: list[float | None] = []

            is_running = True

            def stop(self, *, reason, timeout):
                return None

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)

        runner = COAPRunner(
            session=FakeSession(_fake_spec(), []),
            llm_client=FakeLLM(),
            config=COAPConfig(
                worker_stop_timeout=0.05,
                cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY),
            ),
        )
        errors: list[tuple[str, str]] = []
        workers = [StuckWorker("stuck-worker-1"), StuckWorker("stuck-worker-2")]
        started = time.monotonic()

        runner._stop_workers(
            workers,
            errors,
            timeout=runner.config.worker_stop_timeout,
        )

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(
            [worker.join_timeouts for worker in workers],
            [[runner.config.worker_stop_timeout], [runner.config.worker_stop_timeout]],
        )
        self.assertEqual(
            errors,
            [
                ("stuck-worker-1", "did not stop within worker_stop_timeout"),
                ("stuck-worker-2", "did not stop within worker_stop_timeout"),
            ],
        )

    def test_cleanup_errors_do_not_override_successful_task_outcome(self) -> None:
        result = COAPRunResult(
            outcome=TaskOutcome(TaskStatus.SUCCESS),
            cleanup_errors=[("coap-planning-Alice", "stop timeout")],
        )

        self.assertEqual(result.exit_code, 0)

    def test_config_validates_system_message_fields(self) -> None:
        for kwargs in (
            {"system_messages_enabled": "yes"},
            {"system_message_interval": 0},
            {"system_message_interval": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    COAPConfig(**kwargs)

    def test_record_cache_changes_writes_delete_before_after(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            runner = COAPRunner(
                session=session,
                llm_client=FakeLLM(),
                config=COAPConfig(cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY)),
                episode_recorder=SimpleNamespace(path=tmp),
            )
            try:
                entry = mk_entry(
                    mk_action("wait.wait"),
                    mk_action("navigation.goto_position"),
                    score=0.5,
                )
                entry.task_scope = "task.1"
                runner._record_cache_changes(
                    [
                        CacheEntryChange(
                            event="deduplicate_delete",
                            entry_id=entry.entry_id,
                            before=entry,
                            after=None,
                            source="content_update",
                        )
                    ]
                )
                target = Path(tmp) / "cache" / "changes.jsonl"
                payload = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(payload["event"], "deduplicate_delete")
                self.assertIsNotNone(payload["before"])
                self.assertIsNone(payload["after"])
                self.assertEqual(payload["task_scope"], "task.1")
                self.assertEqual(payload["previous_action"]["name"], "wait.wait")
                self.assertEqual(payload["previous_action_alias"], "wait")
                self.assertEqual(payload["next_action"]["name"], "navigation.goto_position")
                self.assertEqual(payload["next_action_alias"], "goto")
                self.assertEqual(payload["before"]["previous_action_alias"], "wait")
                self.assertEqual(payload["before"]["next_action_alias"], "goto")
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()

    def test_record_cache_changes_writes_aliases_for_score_update(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            runner = COAPRunner(
                session=session,
                llm_client=FakeLLM(),
                config=COAPConfig(cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY)),
                episode_recorder=SimpleNamespace(path=tmp),
            )
            try:
                before = mk_entry(
                    mk_action("wait.wait"),
                    mk_action("navigation.goto_position"),
                    score=0.3,
                )
                before.task_scope = "task.1"
                after = mk_entry(
                    mk_action("wait.wait"),
                    mk_action("navigation.goto_position"),
                    score=0.5,
                )
                after.entry_id = before.entry_id
                after.task_scope = "task.1"
                runner._record_cache_changes(
                    [
                        CacheEntryChange(
                            event="score_update",
                            entry_id=before.entry_id,
                            before=before,
                            after=after,
                            source="score_update",
                        )
                    ]
                )
                target = Path(tmp) / "cache" / "changes.jsonl"
                payload = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(payload["event"], "score_update")
                self.assertEqual(payload["previous_action_alias"], "wait")
                self.assertEqual(payload["next_action_alias"], "goto")
                self.assertEqual(payload["before"]["previous_action_alias"], "wait")
                self.assertEqual(payload["after"]["next_action_alias"], "goto")
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()

    def test_boot_records_cache_changes_file_and_summary_event(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        boot_response = json.dumps(
            {
                "entries": [
                    {
                        "index": 1,
                        "previous_action": "wait",
                        "next_action": "goto",
                        "reason": "需要移动到目标位置",
                        "initial_score": 0.5,
                    }
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            events: list[tuple[str, dict]] = []
            recorder = SimpleNamespace(
                path=tmp,
                event=lambda event_type, **data: events.append((event_type, data)),
            )
            runner = COAPRunner(
                session=session,
                llm_client=FakeLLM([boot_response]),
                config=COAPConfig(
                    cache_boot=CacheBootConfig(
                        mode=CacheBootMode.LLM_INIT,
                        target_entry_count=1,
                    )
                ),
                episode_recorder=recorder,
            )
            try:
                runner._boot_cache()
                changes_path = Path(tmp) / "cache" / "changes.jsonl"
                self.assertTrue(changes_path.is_file())
                lines = [
                    json.loads(line)
                    for line in changes_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["sequence"], 1)
                self.assertEqual(lines[0]["event"], "boot_create")
                self.assertIsNone(lines[0]["before"])
                self.assertIsNotNone(lines[0]["after"])
                self.assertEqual(
                    lines[0]["previous_action"]["name"],
                    "wait.wait",
                )
                self.assertEqual(lines[0]["previous_action_alias"], "wait")
                self.assertEqual(lines[0]["next_action_alias"], "goto")
                self.assertEqual(lines[0]["after"]["previous_action_alias"], "wait")
                self.assertEqual(lines[0]["after"]["next_action_alias"], "goto")
                self.assertEqual(lines[0]["llm_call_count"], 1)
                self.assertEqual(events[-1][0], "cache.booted")
                self.assertEqual(events[-1][1]["change_count"], 1)
                self.assertEqual(
                    events[-1][1]["changes_file"],
                    "cache/changes.jsonl",
                )
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()

    def test_start_run_close_end_to_end(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        runner = COAPRunner(
            session=session,
            llm_client=FakeLLM(
                [
                    "<reason>r</reason><action>wait(seconds=1)</action>"
                ]
            ),
            config=COAPConfig(
                max_run_seconds=0.5,
                cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY),
            ),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )
        result = runner.run()
        self.assertTrue(session.started)
        self.assertTrue(session.closed)
        self.assertEqual(result.stop_reason, "max_run_seconds")
        self.assertEqual(result.cache_stats["entry_count"], 0)
        self.assertGreaterEqual(runner.status()["planned_count"], 0)
        self.assertTrue(runner.status()["agents"]["Alice"]["coap-acting-Alice"]["running"] is False)

    def test_start_without_cache_reasoning_optimization(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        runner = COAPRunner(
            session=session,
            llm_client=FakeLLM(),
            config=COAPConfig(
                max_run_seconds=0.2,
                cache_reasoning_optimization_enabled=False,
                cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY),
            ),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )

        result = runner.run()

        self.assertIsNone(result.runner_error)
        self.assertTrue(session.started)
        self.assertTrue(session.closed)
        self.assertIsNone(runner.cache_service.content_updater)

    def test_cache_stats_use_current_episode_counts_without_double_counting(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        runner = COAPRunner(
            session=session,
            llm_client=FakeLLM(),
            config=COAPConfig(
                cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY)
            ),
        )
        previous = mk_action("wait.wait")
        next_action = mk_action("navigation.goto_position")
        entry = mk_entry(previous, next_action, score=0.5, task_scope="task.1")
        entry.hit_count = 10
        entry.attempt_count = 10
        entry.success_count = 9
        runner.cache_service.repository.upsert(entry)

        runner.score_updater.record_hits([entry.entry_id])
        for index in range(5):
            runner.score_updater.record_feedback(
                entry.entry_id,
                feedback_id=f"direct-{index}",
                success=index < 4,
            )

        def record(decision_id: str, status: CandidateStatus) -> CandidateRecord:
            return CandidateRecord(
                decision_id=decision_id,
                agent_id="Alice",
                previous_action=previous,
                next_action=next_action,
                reasoning="reason",
                status=status,
            )

        runner.cache_service.content_updater._content_generator = FakeContentGenerator()
        content_records = [
            record("content-success", CandidateStatus.SUCCESS),
            record("content-failure", CandidateStatus.FAILURE),
        ]
        aggregated = runner.cache_service.aggregator.aggregate(content_records)
        runner.cache_service.content_updater.apply(
            aggregated,
            task_scope="task.1",
        )

        flush_records = [
            record("flush-success-1", CandidateStatus.SUCCESS),
            record("flush-success-2", CandidateStatus.SUCCESS),
            record("flush-failure", CandidateStatus.FAILURE),
        ]
        runner.cache_service.apply_existing_statistics(
            flush_records,
            task_scope="task.1",
        )

        self.assertEqual(
            runner._cache_stats(),
            {
                "entry_count": 1,
                "hit_count": 1,
                "attempt_count": 10,
                "success_count": 7,
                "buffer_size": 0,
                "update_count": 0,
                "retrieval_count": 0,
                "cache_miss_count": 0,
                "planning_call_count": 0,
                "planning_success_count": 0,
                "planning_failure_count": 0,
                "budget_exhausted_count": 0,
            },
        )
        current = runner.cache_service.repository.get(entry.entry_id)
        self.assertEqual(current.hit_count, 11)
        self.assertEqual(current.attempt_count, 20)
        self.assertEqual(current.success_count, 16)

    def test_system_message_worker_is_disabled_by_default(self) -> None:
        class CountingManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "unused"

        manager = CountingManager()
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        session.manager = manager
        runner = COAPRunner(
            session=session,
            llm_client=FakeLLM(),
            config=COAPConfig(
                max_run_seconds=0.2,
                cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY),
            ),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )

        runner.run()

        self.assertEqual(manager.calls, 0)
        self.assertIsNone(runner._system_message_worker)

    def test_close_is_idempotent(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        runner = COAPRunner(
            session=session,
            llm_client=FakeLLM(),
            config=COAPConfig(cache_boot=CacheBootConfig(mode=CacheBootMode.EMPTY)),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )
        runner.start()
        runner.close()
        runner.close()
        self.assertTrue(session.closed)
        self.assertTrue(runner._closed)

    def test_shared_cache_snapshot_exported(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            runner = COAPRunner(
                session=session,
                llm_client=FakeLLM(),
                config=COAPConfig(),
                episode_recorder=SimpleNamespace(path=tmp),
            )
            try:
                runner.cache_service.repository.upsert(
                    mk_entry(
                        mk_action("wait.wait"),
                        mk_action("navigation.goto_position"),
                        score=0.4,
                    )
                )
                runner._export_cache_snapshot()
                target = Path(tmp) / "cache" / "shared.json"
                self.assertTrue(target.is_file())
                payload = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(payload["sharing"], "shared")
                self.assertEqual(payload["entry_count"], 1)
                self.assertEqual(
                    payload["entries"][0]["next_action"]["name"],
                    "navigation.goto_position",
                )
                self.assertNotIn("previous_action_alias", payload["entries"][0])
                self.assertNotIn("next_action_alias", payload["entries"][0])
                self.assertEqual(payload["entries"][0]["score"], 0.4)
                bundle = load_shared_cache_snapshot(target)
                self.assertEqual(bundle.layout, "shared")
                self.assertEqual(len(bundle.shared_entries), 1)
                self.assertEqual(
                    bundle.shared_entries[0].next_action.name,
                    "navigation.goto_position",
                )
                self.assertEqual(bundle.shared_entries[0].score, 0.4)
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()

    def test_per_agent_does_not_export_cache(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(_fake_spec(), [agent])
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            runner = COAPRunner(
                session=session,
                llm_client=FakeLLM(),
                config=COAPConfig(cache_sharing=CacheSharingMode.PER_AGENT),
                episode_recorder=SimpleNamespace(path=tmp),
            )
            try:
                runner.cache_service.repository.upsert(
                    mk_entry(
                        mk_action("wait.wait"),
                        mk_action("navigation.goto_position"),
                    )
                )
                runner._export_cache_snapshot()
                self.assertFalse((Path(tmp) / "cache").exists())
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()


if __name__ == "__main__":
    unittest.main()
