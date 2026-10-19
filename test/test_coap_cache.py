"""COAP 第一阶段：动作缓存模块单元测试（同步、全离线）。"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.COAP import (  # noqa: E402
    Action,
    ActionSpec,
    AggregatedCandidate,
    BufferUpdateConfig,
    CacheActionCandidatePolicy,
    CacheBootConfig,
    CacheBootError,
    CacheBootMode,
    CacheContentParser,
    CacheContentUpdater,
    CacheColdStartMode,
    CacheConflictError,
    CacheEntry,
    CacheInitializationBatch,
    CacheReason,
    CacheRetriever,
    CacheSource,
    CacheService,
    CandidateAggregator,
    CandidateBuffer,
    CandidateRecord,
    CandidateStatus,
    COAPPromptBuilder,
    COAPProtocolError,
    InMemoryCacheRepository,
    ModelCacheContentGenerator,
    ModelCacheInitializer,
    ParsedActionResponse,
    ReasonSamplingPolicy,
    RetrievalConfig,
    ScoreConfig,
    TaskContext,
    TieBreakPolicy,
    format_action_instructions,
    parse_action_response,
)
from src.baseline.COAP.planning import CacheBootParser, LLMResponse  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 1000.0, mono: float = 0.0) -> None:
        self._now = now
        self._mono = mono

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds


class FakeInitializer:
    def __init__(self, batches) -> None:
        self.batches = list(batches)
        self.calls: list[dict] = []

    def initialize(self, task_context, action_space, *, maximum_entries, remaining_token_budget):
        self.calls.append(
            {
                "task_id": task_context.task_id,
                "maximum_entries": maximum_entries,
                "remaining_token_budget": remaining_token_budget,
            }
        )
        return (
            self.batches.pop(0)
            if self.batches
            else CacheInitializationBatch(entries=(), complete=True)
        )


def _content_result(
    reason: str,
    *,
    initial_score: float | None,
) -> "CacheContentResult":
    from src.baseline.COAP import CacheContentResult

    return CacheContentResult(
        reason=reason,
        initial_score=initial_score,
        input_tokens=10,
        output_tokens=5,
        model_name="fake",
    )


class FakeContentGenerator2:
    """按调用返回可配置内容结果。"""

    def __init__(self, *, reason: str = "抽象原因", initial_score: float = 0.5) -> None:
        self.reason = reason
        self.initial_score = initial_score
        self.calls: list[dict] = []

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
        self.calls.append(
            {
                "previous_action": previous_action.name,
                "next_action": next_action.name,
                "reasons": tuple(reasons),
                "require_initial_score": require_initial_score,
            }
        )
        score = self.initial_score if require_initial_score else None
        return _content_result(self.reason, initial_score=score)


def mk_action(name: str, **params) -> Action:
    return Action(name=name, parameters=params or {})


def mk_entry(
    previous: Action,
    next_action: Action,
    *,
    score: float = 0.5,
    source: CacheSource = CacheSource.LLM,
    task_scope: str | None = None,
    owner: str | None = None,
) -> CacheEntry:
    return CacheEntry(
        previous_action=previous,
        next_action=next_action,
        reason="reason",
        score=score,
        source=source,
        task_scope=task_scope,
        owner_agent_id=owner,
    )


def mk_record(
    previous: Action,
    next_action: Action,
    *,
    status: CandidateStatus,
    decision_id: str = "d1",
    agent_id: str = "Alice",
    reasoning: str = "reasoning",
    created_at: float = 100.0,
) -> CandidateRecord:
    return CandidateRecord(
        decision_id=decision_id,
        agent_id=agent_id,
        previous_action=previous,
        next_action=next_action,
        reasoning=reasoning,
        status=status,
        created_at=created_at,
    )


def mk_task(action_names: tuple[str, ...] = ("wait", "goto", "fight")) -> TaskContext:
    return TaskContext(
        task_id="task.1",
        goal="测试目标",
        action_space=tuple(ActionSpec(name=name) for name in action_names),
    )


def make_service(
    *,
    boot_config: CacheBootConfig | None = None,
    buffer_config: BufferUpdateConfig | None = None,
    initializer=None,
    content_generator=None,
    clock: FakeClock | None = None,
) -> CacheService:
    return CacheService(
        InMemoryCacheRepository(),
        boot_config=boot_config,
        buffer_config=buffer_config,
        score_config=ScoreConfig(minimum_llm_initial_score=0.05),
        clock=clock,
        initializer=initializer,
        content_generator=content_generator,
    )


class CacheBootTests(unittest.TestCase):
    def test_default_mode_resolves_to_llm_init(self) -> None:
        config = CacheBootConfig()
        self.assertEqual(config.mode, CacheBootMode.LLM_INIT)
        self.assertEqual(config.resolved_mode, CacheBootMode.LLM_INIT)
        self.assertEqual(config.target_entry_count, 10)

    def test_empty_mode_initializes_no_entries(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(mode=CacheBootMode.EMPTY)
        )
        result = service.boot.boot(
            mk_task(), (ActionSpec(name="wait"),), task_scope="t"
        )
        self.assertEqual(result.mode, CacheBootMode.EMPTY.value)
        self.assertEqual(result.entries, ())
        self.assertEqual(result.created_count, 0)
        self.assertEqual(service.repository.list_entries(), ())

    def test_llm_init_creates_filtered_entries(self) -> None:
        task = mk_task(("wait", "goto"))
        batch = CacheInitializationBatch(
            entries=(
                mk_entry(mk_action("wait"), mk_action("goto"), score=0.5),
                mk_entry(mk_action("unknown_a"), mk_action("wait"), score=0.4),
                mk_entry(mk_action("wait"), mk_action("unknown_b"), score=0.4),
            ),
            input_tokens=30,
            output_tokens=10,
            complete=True,
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=1,
            ),
            initializer=FakeInitializer([batch]),
        )
        result = service.boot.boot(
            task, task.action_space, task_scope="t"
        )
        self.assertEqual(result.llm_call_count, 1)
        self.assertEqual(result.filtered_count, 2)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].next_action.name, "goto")
        self.assertEqual(result.entries[0].score, 0.5)
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.changes[0].event, "boot_create")

    def test_boot_does_not_persist_start_as_previous_action(self) -> None:
        task = mk_task(("wait", "goto"))
        start_entry = mk_entry(mk_action("START"), mk_action("goto"))
        batch = CacheInitializationBatch(entries=(start_entry,), complete=True)
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=1,
            ),
            initializer=FakeInitializer([batch]),
        )

        result = service.boot.boot(task, task.action_space, task_scope="t")

        self.assertEqual(result.entries, ())
        self.assertEqual(service.repository.list_entries(task_scope="t"), ())
        self.assertEqual(result.filtered_count, 1)

    def test_llm_init_uses_one_call_for_target_count(self) -> None:
        task = mk_task(("wait", "goto"))
        initializer = FakeInitializer(
            [
                CacheInitializationBatch(
                    entries=(
                        mk_entry(mk_action("wait"), mk_action("goto")),
                        mk_entry(mk_action("goto"), mk_action("wait")),
                    ),
                    complete=True,
                ),
            ]
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=2,
            ),
            initializer=initializer,
        )
        result = service.boot.boot(task, task.action_space)
        self.assertEqual(result.llm_call_count, 1)
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(len(initializer.calls), 1)
        self.assertEqual(initializer.calls[0]["maximum_entries"], 2)

    def test_llm_init_keeps_entries_when_single_call_underfills_target(self) -> None:
        task = mk_task(("wait", "goto"))
        initializer = FakeInitializer(
            [
                CacheInitializationBatch(
                    entries=(mk_entry(mk_action("wait"), mk_action("goto")),),
                    complete=False,
                ),
                CacheInitializationBatch(
                    entries=(mk_entry(mk_action("goto"), mk_action("wait")),),
                    complete=True,
                ),
            ]
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=2,
                initialization_call_budget=3,
            ),
            initializer=initializer,
        )
        result = service.boot.boot(task, task.action_space)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(len(initializer.calls), 1)

    def test_reuse_old_filters_unknown_actions(self) -> None:
        task = mk_task(("wait", "goto"))
        old = (
            mk_entry(mk_action("wait"), mk_action("goto"), score=0.9),
            mk_entry(mk_action("old"), mk_action("wait"), score=0.4),
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            )
        )
        result = service.boot.boot(
            task, task.action_space, task_scope="t", old_entries=old
        )
        self.assertEqual(result.reused_count, 1)
        self.assertEqual(result.filtered_count, 1)
        self.assertEqual(result.entries[0].previous_action.name, "wait")
        self.assertEqual(result.changes[0].event, "boot_reuse")

    def test_reuse_previous_first_episode_falls_back_to_llm_init(self) -> None:
        task = mk_task(("wait", "goto"))
        initializer = FakeInitializer(
            [
                CacheInitializationBatch(
                    entries=(mk_entry(mk_action("wait"), mk_action("goto")),),
                    complete=True,
                )
            ]
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_PREVIOUS,
                target_entry_count=1,
            ),
            initializer=initializer,
        )
        result = service.boot.boot(task, task.action_space)
        self.assertEqual(result.mode, CacheBootMode.LLM_INIT.value)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.reused_count, 0)
        self.assertEqual(len(initializer.calls), 1)

    def test_reuse_previous_reuses_injected_old_entries(self) -> None:
        task = mk_task(("wait", "goto"))
        old_entry = mk_entry(mk_action("wait"), mk_action("goto"), score=0.9)
        old_entry.hit_count = 10
        old_entry.attempt_count = 10
        old_entry.success_count = 9
        old = (old_entry,)
        service = make_service(
            boot_config=CacheBootConfig(mode=CacheBootMode.REUSE_PREVIOUS)
        )
        result = service.boot.boot(task, task.action_space, old_entries=old)
        self.assertEqual(result.mode, CacheBootMode.REUSE_PREVIOUS.value)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.reused_count, 1)
        self.assertEqual(result.changes[0].event, "boot_reuse")
        reused = service.repository.get(old_entry.entry_id)
        self.assertIsNotNone(reused)
        self.assertEqual(reused.hit_count, 10)
        self.assertEqual(reused.attempt_count, 10)
        self.assertEqual(reused.success_count, 9)

    def test_score_floor_applied_for_llm_init(self) -> None:
        task = mk_task(("wait", "goto"))
        batch = CacheInitializationBatch(
            entries=(mk_entry(mk_action("wait"), mk_action("goto"), score=0.01),),
            complete=True,
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=1,
            ),
            initializer=FakeInitializer([batch]),
        )
        result = service.boot.boot(task, task.action_space)
        self.assertEqual(result.entries[0].score, 0.05)

    def test_duplicate_pairs_deduplicated(self) -> None:
        task = mk_task(("wait", "goto"))
        batch = CacheInitializationBatch(
            entries=(
                mk_entry(mk_action("wait"), mk_action("goto"), score=0.5),
                mk_entry(mk_action("wait"), mk_action("goto"), score=0.3),
            ),
            complete=True,
        )
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.LLM_INIT,
                target_entry_count=1,
            ),
            initializer=FakeInitializer([batch]),
        )
        result = service.boot.boot(task, task.action_space)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.entries[0].score, 0.5)
        self.assertIn(
            "deduplicate_delete",
            [change.event for change in result.changes],
        )


class CacheRetrieverTests(unittest.TestCase):
    def test_retrieve_top_k_sorted_by_score(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            )
        )
        task = mk_task(("wait", "goto", "fight"))
        old = (
            mk_entry(mk_action("wait"), mk_action("fight"), score=0.3),
            mk_entry(mk_action("wait"), mk_action("goto"), score=0.9),
            mk_entry(mk_action("goto"), mk_action("wait"), score=0.7),
        )
        service.boot.boot(
            task, task.action_space, task_scope="t", old_entries=old
        )
        result = service.retrieve(mk_action("wait"), task_scope="t", agent_id="Alice")
        self.assertTrue(result.hit)
        self.assertEqual([e.next_action.name for e in result.entries], ["goto", "fight"])
        self.assertEqual(result.matched_count, 2)

    def test_retrieve_start_sentinel(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            )
        )
        task = mk_task(("wait", "goto"))
        old = (
            mk_entry(Action(name="START"), mk_action("goto"), score=0.8),
            mk_entry(mk_action("wait"), mk_action("goto"), score=0.7),
        )
        service.boot.boot(task, task.action_space, task_scope="t", old_entries=old)
        result = service.retrieve(None, task_scope="t", agent_id="Alice")
        self.assertEqual(result.entries, ())
        self.assertEqual(
            [entry.previous_action.name for entry in service.repository.list_entries()],
            ["wait"],
        )

    def test_retrieve_minimum_score_filter(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            ),
        )
        task = mk_task(("wait", "goto", "fight"))
        old = (
            mk_entry(mk_action("wait"), mk_action("goto"), score=0.2),
            mk_entry(mk_action("wait"), mk_action("fight"), score=0.7),
        )
        service.boot.boot(task, task.action_space, task_scope="t", old_entries=old)
        retriever = CacheRetriever(
            service.repository,
            RetrievalConfig(minimum_score=0.5),
            canonicalizer=service.cache_canonicalizer,
        )
        result = retriever.retrieve(mk_action("wait"), task_scope="t")
        self.assertEqual([e.next_action.name for e in result.entries], ["fight"])
        self.assertEqual(result.filtered_count, 1)

    def test_scope_and_owner_filter(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            )
        )
        task = mk_task(("wait", "goto"))
        old = (
            mk_entry(mk_action("wait"), mk_action("goto"), score=0.9, task_scope="t1"),
            mk_entry(mk_action("wait"), mk_action("fight"), score=0.8, task_scope="t2"),
        )
        service.boot.boot(task, task.action_space, task_scope="t1", old_entries=old)
        result = service.retrieve(mk_action("wait"), task_scope="t1", agent_id="Alice")
        self.assertEqual([e.next_action.name for e in result.entries], ["goto"])

    def test_retrieve_requires_previous_when_sentinel_disabled(self) -> None:
        from src.baseline.COAP import CacheRetrievalError

        service = make_service()
        retriever = CacheRetriever(
            service.repository,
            RetrievalConfig(allow_start_sentinel=False),
            canonicalizer=service.cache_canonicalizer,
        )
        with self.assertRaises(CacheRetrievalError):
            retriever.retrieve(None, task_scope="t")

    def test_retrieve_matches_previous_by_name_ignoring_parameters(self) -> None:
        service = make_service(
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            )
        )
        task = mk_task(("fight", "wait"))
        old = (
            mk_entry(
                mk_action("fight", target={"name": "zombie", "entity_id": 74}),
                mk_action("wait"),
                score=0.8,
            ),
        )
        service.boot.boot(task, task.action_space, task_scope="t", old_entries=old)
        result = service.retrieve(
            mk_action("fight", target={"name": "zombie", "entity_id": 79}),
            task_scope="t",
            agent_id="Alice",
        )
        self.assertTrue(result.hit)
        self.assertEqual(result.entries[0].next_action.name, "wait")


class CandidateBufferTests(unittest.TestCase):
    def test_add_duplicate_and_flush_threshold(self) -> None:
        clock = FakeClock()
        buffer = CandidateBuffer(
            BufferUpdateConfig(buffer_size_threshold=2),
            clock=clock,
        )
        prev = mk_action("wait")
        nxt = mk_action("goto")
        first = mk_record(prev, nxt, status=CandidateStatus.SUCCESS, decision_id="d1")
        result = buffer.add(first)
        self.assertTrue(result.accepted)
        self.assertFalse(result.should_flush)
        duplicate = buffer.add(first)
        self.assertFalse(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        second = mk_record(prev, nxt, status=CandidateStatus.SUCCESS, decision_id="d2")
        flushed = buffer.add(second)
        self.assertTrue(flushed.should_flush)
        self.assertEqual(buffer.size(), 2)

    def test_conflicting_decision_raises(self) -> None:
        buffer = CandidateBuffer(BufferUpdateConfig())
        prev = mk_action("wait")
        nxt = mk_action("goto")
        buffer.add(mk_record(prev, nxt, status=CandidateStatus.SUCCESS, decision_id="d1"))
        with self.assertRaises(CacheConflictError):
            buffer.add(
                mk_record(
                    prev,
                    nxt,
                    status=CandidateStatus.FAILURE,
                    decision_id="d1",
                )
            )

    def test_time_based_flush(self) -> None:
        clock = FakeClock()
        buffer = CandidateBuffer(
            BufferUpdateConfig(update_interval_seconds=10.0),
            clock=clock,
        )
        prev = mk_action("wait")
        nxt = mk_action("goto")
        buffer.add(mk_record(prev, nxt, status=CandidateStatus.SUCCESS))
        self.assertFalse(buffer.should_flush())
        clock.advance(11.0)
        self.assertTrue(buffer.should_flush())

    def test_acknowledge_removes_records(self) -> None:
        buffer = CandidateBuffer(BufferUpdateConfig())
        prev = mk_action("wait")
        nxt = mk_action("goto")
        record = mk_record(prev, nxt, status=CandidateStatus.SUCCESS)
        buffer.add(record)
        buffer.acknowledge([record.record_id])
        self.assertEqual(buffer.size(), 0)

    def test_drop_oldest_on_overflow(self) -> None:
        from src.baseline.COAP import BufferOverflowPolicy

        buffer = CandidateBuffer(
            BufferUpdateConfig(
                buffer_size_threshold=1,
                maximum_buffer_records=1,
                buffer_overflow_policy=BufferOverflowPolicy.DROP_OLDEST,
            )
        )
        prev = mk_action("wait")
        nxt = mk_action("goto")
        first = mk_record(prev, nxt, status=CandidateStatus.SUCCESS, decision_id="d1")
        second = mk_record(prev, nxt, status=CandidateStatus.SUCCESS, decision_id="d2")
        buffer.add(first)
        result = buffer.add(second)
        self.assertEqual(result.dropped_record_ids, (first.record_id,))
        self.assertEqual(buffer.size(), 1)


class CandidateAggregatorTests(unittest.TestCase):
    def test_aggregate_counts_and_pending(self) -> None:
        aggregator = CandidateAggregator(BufferUpdateConfig())
        prev = mk_action("wait")
        nxt = mk_action("goto")
        records = (
            mk_record(prev, nxt, status=CandidateStatus.SUCCESS, created_at=1.0),
            mk_record(prev, nxt, status=CandidateStatus.FAILURE, created_at=2.0),
            mk_record(prev, nxt, status=CandidateStatus.UNCERTAIN, created_at=3.0),
        )
        aggregated = aggregator.aggregate(records)
        self.assertEqual(len(aggregated), 1)
        item = aggregated[0]
        self.assertEqual(item.success_count, 1)
        self.assertEqual(item.failure_count, 1)
        self.assertEqual(item.uncertain_count, 1)
        self.assertEqual(item.pending_attempt_count, 2)
        self.assertEqual(item.pending_success_count, 1)
        self.assertEqual(len(item.record_ids), 3)

    def test_aggregate_reason_sampling(self) -> None:
        aggregator = CandidateAggregator(
            BufferUpdateConfig(
                maximum_reasons_per_pair=2,
                reason_sampling_policy=ReasonSamplingPolicy.LATEST,
            )
        )
        prev = mk_action("wait")
        nxt = mk_action("goto")
        records = tuple(
            mk_record(
                prev,
                nxt,
                status=CandidateStatus.SUCCESS,
                reasoning=f"reason-{index}",
                created_at=float(index),
            )
            for index in range(4)
        )
        item = aggregator.aggregate(records)[0]
        self.assertEqual(item.reasons, ("reason-2", "reason-3"))


class CacheContentUpdaterTests(unittest.TestCase):
    def test_create_new_entry_with_initial_score(self) -> None:
        service = make_service(content_generator=FakeContentGenerator2())
        prev = mk_action("wait")
        nxt = mk_action("goto")
        aggregated = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [mk_record(prev, nxt, status=CandidateStatus.SUCCESS)]
        )
        report = service.content_updater.apply(
            aggregated, task_scope="t", owner_agent_id="Alice"
        )
        self.assertEqual(len(report.created_entry_ids), 1)
        entry = report.entries[0]
        self.assertEqual(entry.previous_action.name, "wait")
        self.assertEqual(entry.next_action.name, "goto")
        self.assertEqual(entry.source, CacheSource.ENV)
        self.assertEqual(entry.attempt_count, 1)
        self.assertEqual(entry.success_count, 1)
        self.assertEqual(
            service.content_updater.episode_statistics(),
            {"hit_count": 0, "attempt_count": 1, "success_count": 1},
        )
        self.assertEqual(entry.owner_agent_id, "Alice")
        self.assertEqual(len(report.changes), 1)
        self.assertEqual(report.changes[0].event, "content_create")
        self.assertIsNone(report.changes[0].before)
        self.assertEqual(report.changes[0].after.entry_id, entry.entry_id)

    def test_update_existing_entry_reason(self) -> None:
        generator = FakeContentGenerator2(reason="新原因")
        service = make_service(content_generator=generator)
        prev = mk_action("wait")
        nxt = mk_action("goto")
        first = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [mk_record(prev, nxt, status=CandidateStatus.SUCCESS)]
        )
        service.content_updater.apply(first, task_scope="t")
        second = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [
                mk_record(
                    prev,
                    nxt,
                    status=CandidateStatus.SUCCESS,
                    decision_id="d2",
                )
            ]
        )
        report = service.content_updater.apply(second, task_scope="t")
        self.assertEqual(len(report.updated_entry_ids), 1)
        self.assertEqual(report.entries[0].reason.text, "新原因")
        self.assertEqual(report.entries[0].attempt_count, 2)
        self.assertEqual(
            service.content_updater.episode_statistics(),
            {"hit_count": 0, "attempt_count": 2, "success_count": 2},
        )
        self.assertEqual(generator.calls[-1]["require_initial_score"], False)
        self.assertEqual(len(report.changes), 1)
        self.assertEqual(report.changes[0].event, "content_update")
        self.assertEqual(report.changes[0].before.attempt_count, 1)
        self.assertEqual(report.changes[0].after.attempt_count, 2)

    def test_env_source_preferred_over_llm(self) -> None:
        service = make_service(
            content_generator=FakeContentGenerator2(),
            boot_config=CacheBootConfig(
                mode=CacheBootMode.REUSE_OLD, source_path="old.json"
            ),
        )
        task = mk_task(("wait", "goto"))
        service.boot.boot(
            task,
            task.action_space,
            task_scope="t",
            old_entries=(
                mk_entry(mk_action("wait"), mk_action("goto"), score=0.4),
            ),
        )
        aggregated = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [mk_record(mk_action("wait"), mk_action("goto"), status=CandidateStatus.SUCCESS)]
        )
        service.content_updater.apply(aggregated, task_scope="t")
        entries = service.repository.list_entries(task_scope="t")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].source, CacheSource.ENV)

    def test_uncertain_only_keeps_llm_source_and_default_score(self) -> None:
        service = make_service(
            content_generator=FakeContentGenerator2(initial_score=0.5),
            buffer_config=BufferUpdateConfig(default_uncertain_score=0.5),
        )
        aggregated = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [
                mk_record(
                    mk_action("wait"),
                    mk_action("goto"),
                    status=CandidateStatus.UNCERTAIN,
                )
            ]
        )
        service.content_updater.apply(aggregated, task_scope="t")
        entries = service.repository.list_entries(task_scope="t")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].source, CacheSource.LLM)
        self.assertEqual(entries[0].score, 0.5)

    def test_start_previous_action_is_ignored_by_content_update(self) -> None:
        generator = FakeContentGenerator2()
        service = make_service(content_generator=generator)
        aggregated = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [
                mk_record(
                    mk_action("START"),
                    mk_action("goto"),
                    status=CandidateStatus.SUCCESS,
                    decision_id="d-start",
                )
            ]
        )
        report = service.content_updater.apply(aggregated, task_scope="t")
        self.assertEqual(service.repository.list_entries(task_scope="t"), ())
        self.assertEqual(generator.calls, [])
        self.assertEqual(report.created_entry_ids, ())
        self.assertEqual(report.updated_entry_ids, ())
        self.assertEqual(report.changes, ())
        self.assertEqual(report.llm_call_count, 0)
        self.assertEqual(report.processed_record_ids, aggregated[0].record_ids)


class CacheParsersTests(unittest.TestCase):
    def test_cache_boot_parser(self) -> None:
        parser = CacheBootParser()
        text = (
            '{"entries": [{"index": 1, "previous_action": "wait",'
            ' "next_action": "goto", "reason": "任务需要移动",'
            ' "initial_score": 0.4}]}'
        )
        batch = parser.parse(
            text,
            maximum_entries=1,
            response=LLMResponse(content=text, request_id="r1", input_tokens=5, output_tokens=3),
            action_aliases={"wait": "wait.wait", "goto": "navigation.goto_position"},
        )
        self.assertTrue(batch.complete)
        self.assertEqual(len(batch.entries), 1)
        self.assertEqual(batch.entries[0].previous_action.name, "wait.wait")
        self.assertEqual(batch.entries[0].next_action.name, "navigation.goto_position")
        self.assertEqual(batch.entries[0].previous_action.parameters, {})
        self.assertEqual(batch.entries[0].next_action.parameters, {})
        self.assertEqual(batch.entries[0].score, 0.4)
        self.assertEqual(batch.metadata["request_id"], "r1")

    def test_cache_boot_parser_rejects_invalid_score(self) -> None:
        parser = CacheBootParser()
        text = (
            '{"entries": [{"index": 1, "previous_action": "wait",'
            ' "next_action": "goto", "reason": "r", "initial_score": 1.5}]}'
        )
        batch = parser.parse(text, maximum_entries=1, response=LLMResponse(text))
        self.assertEqual(batch.entries, ())
        self.assertEqual(batch.metadata["dropped_entry_count"], 1)

    def test_cache_boot_parser_rejects_missing_or_wrong_index(self) -> None:
        parser = CacheBootParser()
        text = (
            '{"entries": [{"index": 2, "previous_action": "wait",'
            ' "next_action": "goto", "reason": "r", "initial_score": 0.4}]}'
        )
        batch = parser.parse(text, maximum_entries=1, response=LLMResponse(text))
        self.assertEqual(batch.entries, ())
        self.assertEqual(batch.metadata["dropped_entry_count"], 1)

    def test_cache_boot_parser_recovers_valid_entries_from_broken_response(self) -> None:
        parser = CacheBootParser()
        text = (
            '{"entries": ['
            '{"index": 1, "previous_action": "wait",'
            ' "next_action": "goto", "reason": "valid-1",'
            ' "initial_score": 0.4},'
            '{"index": 2, "previous_action": "wait",'
            ' "next_action": "reason": "broken", "initial_score": 0.4},'
            '{"index": 3, "previous_action": "goto",'
            ' "next_action": "wait", "reason": "valid-3",'
            ' "initial_score": 0.3}'
            ']}'
        )
        batch = parser.parse(
            text,
            maximum_entries=3,
            response=LLMResponse(text),
            action_aliases={"wait": "wait.wait", "goto": "navigation.goto_position"},
        )
        self.assertEqual(len(batch.entries), 2)
        self.assertEqual([entry.reason.text for entry in batch.entries], ["valid-1", "valid-3"])
        self.assertTrue(batch.metadata["recovery_mode"])
        self.assertEqual(batch.metadata["accepted_entry_count"], 2)
        self.assertEqual(batch.metadata["dropped_entry_count"], 1)

    def test_cache_boot_parser_rejects_start_and_parameters(self) -> None:
        parser = CacheBootParser()
        start_batch = parser.parse(
            '{"entries": [{"index": 1, "previous_action": "START",'
            ' "next_action": "goto", "reason": "r", "initial_score": 0.4}]}',
            maximum_entries=1,
            response=LLMResponse(""),
            forbidden_action_names={"START"},
        )
        parameter_batch = parser.parse(
            '{"entries": [{"index": 1, "previous_action": "wait(seconds=1)",'
            ' "next_action": "goto", "reason": "r", "initial_score": 0.4}]}',
            maximum_entries=1,
            response=LLMResponse(""),
        )
        self.assertEqual(start_batch.entries, ())
        self.assertEqual(parameter_batch.entries, ())

    def test_cache_boot_parser_allows_explicit_start_alias_for_compatibility(self) -> None:
        parser = CacheBootParser()
        batch = parser.parse(
            '{"entries": [{"index": 1, "previous_action": "START",'
            ' "next_action": "goto", "reason": "首轮移动", "initial_score": 0.4}]}',
            maximum_entries=1,
            response=LLMResponse(""),
            action_aliases={"START": "START", "goto": "navigation.goto_position"},
            forbidden_next_action_names={"START"},
        )
        self.assertEqual(len(batch.entries), 1)
        self.assertEqual(batch.entries[0].previous_action.name, "START")
        self.assertEqual(
            batch.entries[0].next_action.name,
            "navigation.goto_position",
        )

    def test_cache_content_parser_requires_initial_score_for_new_pair(self) -> None:
        from src.baseline.COAP import PlanningResponseError

        parser = CacheContentParser()
        with self.assertRaises(PlanningResponseError):
            parser.parse(
                '{"reason": "r", "initial_score": null}',
                require_initial_score=True,
                response=LLMResponse(""),
            )

    def test_cache_content_parser_ignores_existing_pair_score(self) -> None:
        parser = CacheContentParser()
        for text in (
            '{"reason": "r", "initial_score": 0.5}',
            '{"reason": "r", "initial_score": 0.0}',
            '{"reason": "r"}',
        ):
            with self.subTest(text=text):
                result = parser.parse(
                    text,
                    require_initial_score=False,
                    response=LLMResponse(""),
                )
                self.assertEqual(result.reason.text, "r")
                self.assertIsNone(result.initial_score)

    def test_cache_content_initial_score_is_limited_to_half(self) -> None:
        from src.baseline.COAP import PlanningResponseError

        parser = CacheContentParser()
        accepted = parser.parse(
            '{"reason": "r", "initial_score": 0.5}',
            require_initial_score=True,
            response=LLMResponse(""),
        )
        self.assertEqual(accepted.initial_score, 0.5)
        for score in (0, -0.1, 0.5001, 1.0):
            with self.subTest(score=score):
                with self.assertRaises(PlanningResponseError):
                    parser.parse(
                        f'{{"reason": "r", "initial_score": {score}}}',
                        require_initial_score=True,
                        response=LLMResponse(""),
                    )
        with self.assertRaises(ValueError):
            _content_result("r", initial_score=0.5001)


class ProtocolTests(unittest.TestCase):
    KEYWORD = "</action>"

    def test_standard_action_parse_with_communication(self) -> None:
        text = "<reason>需要移动</reason><action>goto(x=1)</action><chat>我去坐标</chat>"
        parsed = parse_action_response(
            text,
            operation="standard_action",
        )
        self.assertEqual(parsed.reasoning, "需要移动")
        self.assertEqual(parsed.action_text, "goto(x=1)")
        self.assertEqual(parsed.communication, "我去坐标")

    def test_cache_action_parse_without_communication(self) -> None:
        text = "<action>fight(target={'name': 'zombie'})</action>"
        parsed = parse_action_response(
            text,
            operation="cache_action",
        )
        self.assertEqual(parsed.action_text, "fight(target={'name': 'zombie'})")
        self.assertEqual(parsed.communication, "")

    def test_timed_standard_action_parses_interrupt(self) -> None:
        text = (
            "<reason>紧急</reason><interrupt>true</interrupt>"
            "<action>wait(seconds=5)</action>"
        )
        parsed = parse_action_response(text, operation="timed_standard_action")
        self.assertTrue(parsed.interrupt)
        self.assertEqual(parsed.action_text, "wait(seconds=5)")

    def test_markdown_fence_tolerated(self) -> None:
        text = "```\n<action>fight(target={'name': 'zombie'})</action><chat>hi</chat>\n```"
        parsed = parse_action_response(
            text,
            operation="cache_action",
        )
        self.assertEqual(parsed.communication, "hi")

    def test_missing_action_tag_raises(self) -> None:
        text = "<action>fight()</action><action>observe()</action>"
        with self.assertRaises(COAPProtocolError):
            parse_action_response(text, operation="cache_action")

    def test_multiline_reasoning_allowed(self) -> None:
        text = "<reason>第一行\n第二行</reason><action>goto(x=1)</action>"
        parsed = parse_action_response(text, operation="standard_action")
        self.assertIn("第二行", parsed.reasoning)

    def test_format_instructions(self) -> None:
        standard = format_action_instructions("standard_action")
        self.assertIn(
            "高效、简洁进行快速推理，根据所给信息得出下一步动作的最优选择。",
            standard,
        )
        self.assertIn("<reason>", standard)
        self.assertIn("<action>", standard)
        timed = format_action_instructions("timed_standard_action")
        self.assertIn(
            "高效、简洁进行快速推理，根据所给信息得出下一步动作的最优选择。",
            timed,
        )
        self.assertIn("<interrupt>", timed)
        self.assertLess(timed.index("<interrupt>"), timed.index("<action>"))
        cache = format_action_instructions(
            "cache_action", cache_candidate_instruction="必须匹配候选。"
        )
        self.assertIn("必须匹配候选。", cache)


class PromptBuilderTests(unittest.TestCase):
    def test_build_action_injects_cache_section_only_for_cache_path(self) -> None:
        builder = COAPPromptBuilder()
        entry = mk_entry(mk_action("wait"), mk_action("fight"), score=0.9)
        _, user = builder.build_action(
            operation="cache_action",
            objective="目标",
            catalog="wait, fight",
            agent_id="Alice",
            team="Bob",
            observation_text="obs",
            history_text="chat",
            error_text="none",
            cache_candidates=[entry],
        )
        self.assertIn("【缓存候选】", user["content"])
        self.assertIn("fight", user["content"])
        _, standard_user = builder.build_action(
            operation="standard_action",
            objective="目标",
            catalog="wait, fight",
            agent_id="Alice",
            team="Bob",
            observation_text="obs",
            history_text="chat",
            error_text="none",
        )
        self.assertNotIn("【缓存候选】", standard_user["content"])


class CacheRepresentationDefaultTests(unittest.TestCase):
    def test_default_strips_parameters_and_renders_name_only(self) -> None:
        service = make_service(
            content_generator=FakeContentGenerator2(initial_score=0.5)
        )
        aggregated = CandidateAggregator(BufferUpdateConfig()).aggregate(
            [
                mk_record(
                    mk_action("attack_zombie", target="z1"),
                    mk_action("pickup_item", item="diamond"),
                    status=CandidateStatus.SUCCESS,
                )
            ]
        )
        service.content_updater.apply(aggregated, task_scope="t")
        entries = service.repository.list_entries(task_scope="t")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].previous_action.parameters, {})
        self.assertEqual(entries[0].next_action.parameters, {})

        builder = COAPPromptBuilder()
        _, user = builder.build_action(
            operation="cache_action",
            objective="目标",
            catalog="attack_zombie, pickup_item",
            agent_id="Alice",
            team="Bob",
            observation_text="obs",
            history_text="chat",
            error_text="none",
            cache_candidates=entries,
        )
        self.assertIn(
            "- 前序: attack_zombie -> 后继: pickup_item (评分",
            user["content"],
        )
        self.assertNotIn("pickup_item({", user["content"])


class RepositoryTests(unittest.TestCase):
    def test_crud_and_filters(self) -> None:
        repo = InMemoryCacheRepository()
        entry = mk_entry(
            mk_action("wait"), mk_action("goto"), task_scope="t", owner="Alice"
        )
        repo.upsert(entry)
        self.assertEqual(repo.get(entry.entry_id), entry)
        self.assertEqual(len(repo.list_entries(task_scope="t")), 1)
        self.assertEqual(len(repo.list_entries(task_scope="other")), 0)
        self.assertEqual(
            len(repo.list_entries(task_scope="t", owner_agent_id="Alice", match_owner=True)),
            1,
        )
        self.assertTrue(repo.delete(entry.entry_id))
        self.assertIsNone(repo.get(entry.entry_id))

    def test_find_by_previous_key(self) -> None:
        repo = InMemoryCacheRepository()
        previous = mk_action("wait")
        entry = mk_entry(previous, mk_action("goto"))
        canonical = entry.previous_action
        repo.upsert(entry)
        found = repo.find_by_previous_key(canonical.canonical_key)
        self.assertEqual(len(found), 1)

    def test_replace_all_rejects_duplicate_ids(self) -> None:
        from src.baseline.COAP import RepositoryPortError

        repo = InMemoryCacheRepository()
        entry = mk_entry(mk_action("wait"), mk_action("goto"))
        with self.assertRaises(RepositoryPortError):
            repo.replace_all([entry, entry])


class ModelPortTests(unittest.TestCase):
    def test_model_cache_initializer_builds_and_parses(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages = None

            def complete(self, messages, *, metadata):
                self.messages = messages
                return LLMResponse(
                    content=(
                        '{"entries": [{"index": 1, "previous_action": "wait",'
                        ' "next_action": "goto", "reason": "r",'
                        ' "initial_score": 0.4}]}'
                    ),
                    request_id="req-1",
                    model="fake",
                    input_tokens=4,
                    output_tokens=2,
                )

        client = FakeClient()
        builder = COAPPromptBuilder()
        initializer = ModelCacheInitializer(
            client, builder, CacheBootParser()
        )
        task = TaskContext(
            task_id="task.1",
            goal="测试目标",
            action_space=(
                ActionSpec(
                    name="wait.wait",
                    description="等待一段时间",
                    metadata={"cache_boot_action_name": "wait"},
                ),
                ActionSpec(
                    name="navigation.goto_position",
                    description="移动到目标",
                    metadata={"cache_boot_action_name": "goto"},
                ),
            ),
        )
        batch = initializer.initialize(
            task, task.action_space, maximum_entries=1, remaining_token_budget=100
        )
        self.assertEqual(len(batch.entries), 1)
        self.assertEqual(batch.entries[0].previous_action.name, "wait.wait")
        self.assertEqual(batch.entries[0].next_action.name, "navigation.goto_position")
        self.assertEqual(batch.entries[0].next_action.parameters, {})
        self.assertEqual(batch.metadata["request_id"], "req-1")
        self.assertIn("entries", client.messages[0]["content"])
        self.assertIn("previous_action", client.messages[0]["content"])
        self.assertIn("【动作空间】", client.messages[1]["content"])
        self.assertIn("- wait", client.messages[1]["content"])
        self.assertNotIn("wait.wait", client.messages[1]["content"])
        self.assertNotIn("等待一段时间", client.messages[1]["content"])
        self.assertIn("组合 1 条", client.messages[1]["content"])

    def test_model_cache_content_generator_builds_and_parses(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages = None
                self.metadata = None

            def complete(self, messages, *, metadata):
                self.messages = messages
                self.metadata = dict(metadata)
                return LLMResponse(
                    content='{"reason": "抽象原因", "initial_score": 0.5}',
                    request_id="req-2",
                    model="fake",
                    input_tokens=4,
                    output_tokens=2,
                )

        client = FakeClient()
        builder = COAPPromptBuilder()
        task = TaskContext(
            task_id="task.2",
            goal="清剿怪物并保护队伍",
            action_space=(
                ActionSpec(
                    name="wait.wait",
                    description="等待队友或怪物进入合适距离",
                    metadata={"cache_boot_action_name": "wait"},
                ),
                ActionSpec(
                    name="navigation.goto_position",
                    description="移动到指定位置或目标附近",
                    metadata={"cache_boot_action_name": "goto"},
                ),
            ),
        )
        generator = ModelCacheContentGenerator(
            client, builder, CacheContentParser(), task_context=task
        )
        result = generator.generate(
            mk_action("wait.wait", seconds=1),
            mk_action("navigation.goto_position", x=1, y=2, z=3),
            ["reason-1", "reason-2"],
            require_initial_score=True,
        )
        self.assertEqual(result.reason.text, "抽象原因")
        self.assertEqual(result.initial_score, 0.5)
        self.assertEqual(result.metadata["request_id"], "req-2")
        self.assertEqual(client.metadata["previous_action"], "wait")
        self.assertEqual(client.metadata["next_action"], "goto")
        system_text = client.messages[0]["content"]
        user_text = client.messages[1]["content"]
        self.assertIn("(0,0.5]", system_text)
        self.assertIn("reason不得出现具体实体编号标识。", system_text)
        self.assertNotIn("(0,1]", system_text)
        self.assertIn("(0,0.5]", user_text)
        self.assertNotIn("(0,.5]", user_text)
        self.assertIn("清剿怪物并保护队伍", user_text)
        self.assertIn("前序动作: wait", user_text)
        self.assertIn("后继动作: goto", user_text)
        self.assertIn("等待队友或怪物进入合适距离", user_text)
        self.assertIn("移动到指定位置或目标附近", user_text)
        self.assertIn("1. reason-1", user_text)
        self.assertIn("2. reason-2", user_text)
        self.assertNotIn("wait.wait", user_text)
        self.assertNotIn("navigation.goto_position", user_text)

    def test_cache_calls_carry_agent_identifier(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.metadata: list[dict] = []

            def complete(self, messages, *, metadata):
                self.metadata.append(dict(metadata))
                content = (
                    '{"entries": [{"index": 1, "previous_action": "wait",'
                    ' "next_action": "goto", "reason": "r", "initial_score": 0.5}]}'
                    if metadata.get("operation") == "cache_boot"
                    else '{"reason": "r", "initial_score": 0.5}'
                )
                return LLMResponse(
                    content=content,
                    request_id="r",
                    model="fake",
                )

        builder = COAPPromptBuilder()
        task = mk_task(("wait", "goto"))
        boot_client = RecordingClient()
        initializer = ModelCacheInitializer(
            boot_client, builder, CacheBootParser()
        )
        initializer.initialize(
            task,
            task.action_space,
            maximum_entries=1,
            remaining_token_budget=100,
        )
        self.assertEqual(boot_client.metadata[0]["agent_id"], "shared")

        update_client = RecordingClient()
        generator = ModelCacheContentGenerator(
            update_client, builder, CacheContentParser()
        )
        generator.generate(
            mk_action("wait"),
            mk_action("goto"),
            ["r"],
            owner_agent_id="Alice",
            require_initial_score=True,
        )
        generator.generate(
            mk_action("wait"),
            mk_action("goto"),
            ["r"],
            require_initial_score=True,
        )
        self.assertEqual(update_client.metadata[0]["agent_id"], "Alice")
        self.assertEqual(update_client.metadata[1]["agent_id"], "shared")

    def test_llm_port_adapter_sets_cache_source(self) -> None:
        from src.baseline.COAP.runner import _LLMClientPortAdapter

        captured: dict = {}

        class FakeClient:
            def complete(self, request):
                captured["source"] = request.source
                return SimpleNamespace(
                    content="x",
                    request_id="r",
                    model="m",
                    usage={},
                )

        adapter = _LLMClientPortAdapter(FakeClient())
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            metadata={
                "operation": "cache_content_update",
                "agent_id": "shared",
                "task_id": "task.1",
            },
        )
        source = captured["source"]
        self.assertEqual(source.component, "coap_cache")
        self.assertEqual(source.operation, "cache_content_update")
        self.assertEqual(source.agent_id, "shared")
        self.assertEqual(source.task_id, "task.1")


class CacheServiceTests(unittest.TestCase):
    def test_partition_candidates_by_sharing_mode(self) -> None:
        shared = make_service()
        prev = mk_action("wait")
        nxt = mk_action("goto")
        records = (
            mk_record(prev, nxt, status=CandidateStatus.SUCCESS, agent_id="Alice"),
            mk_record(prev, nxt, status=CandidateStatus.SUCCESS, agent_id="Bob"),
        )
        self.assertEqual(
            shared.partition_candidates(records),
            ((None, tuple(records)),),
        )
        self.assertEqual(shared.maintenance_owners(["Alice", "Bob"]), (None,))

    def test_owner_for_per_agent(self) -> None:
        from src.baseline.COAP import CacheSharingMode

        service = CacheService(
            InMemoryCacheRepository(),
            sharing_mode=CacheSharingMode.PER_AGENT,
        )
        self.assertEqual(service.owner_for("Alice"), "Alice")
        self.assertEqual(
            service.maintenance_owners(["Bob", "Alice"]),
            ("Alice", "Bob"),
        )


if __name__ == "__main__":
    unittest.main()
