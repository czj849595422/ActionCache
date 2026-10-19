"""COAP runner：四条并行线路的装配、启动、等待与清理。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Sequence
from uuid import uuid4

from src.agents.action import SimpleInstructionRegistry
from src.llm import LLMCallSource, LLMRequest
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory
from src.perception import (
    BasePerception,
    RconBlockEntityProvider,
    TASK_PERCEPTION_PROFILES,
    task_perception,
    task_verbalizer,
    world_snapshot,
    world_verbalizer,
)
from src.perception.rule.composition import ChainRule
from src.recording import EpisodeRecorder
from src.recording.table import ActionExecutionTable, make_call_file_resolver
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer
from src.tasks import (
    SystemMessageWorker,
    TaskEnvironmentBundle,
    TaskEnvironmentSession,
    TaskStatus,
    apply_task_runtime_overrides,
)
from src.tasks.spec import task_description_text
from src.tasks.trade import create_trade_recipe_history
from src.launcher.broadcast_dispatcher import AsyncBroadcastDispatcher

from src.baseline.Parallelized.budget import RunBudget
from src.baseline.Parallelized.workers import ObservationWorker

from .cache import CacheService
from .codec import COAPDecisionCodec
from .config import COAPConfig, CacheSharingMode
from .consistency import ImportantParameterActionConsistency
from .planning import (
    CacheBootParser,
    CacheContentParser,
    LLMResponse,
    ModelCacheContentGenerator,
    ModelCacheInitializer,
)
from .prompts import COAPPromptBuilder
from .repository import InMemoryCacheRepository
from .schemas import (
    Action,
    ActionSpec,
    CacheEntry,
    CacheEntryChange,
    CacheReason,
    CacheSource,
    CacheSourceBundle,
    COAPRunResult,
    TaskContext,
)
from .scoring import ScoreUpdater
from .workers import (
    COAPActingWorker,
    COAPCachePlanningWorker,
    COAPCacheWorker,
    COAPDecisionCoordinator,
    COAPPlanningWorker,
)


ObservationComponentsFactory = Callable[[Any, Callable[[], Any]], tuple[Any, Any]]
RuntimeCloser = Callable[[], None]


class _LLMClientPortAdapter:
    """把项目 LLMClient 包装为 COAP planning 的 LLMClientPort。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        metadata: dict[str, Any],
        stop_event: Any | None = None,
    ) -> LLMResponse:
        agent_id = str(metadata.get("agent_id") or "")
        operation = str(metadata.get("operation") or "cache")
        task_id = str(metadata.get("task_id") or "")
        request = LLMRequest(
            messages=tuple(messages),
            metadata=dict(metadata),
            source=LLMCallSource(
                component="coap_cache",
                operation=operation,
                task_id=task_id,
                agent_id=agent_id or None,
            ),
        )
        if stop_event is None:
            response = self._client.complete(request)
        else:
            response = self._client.complete(request, stop_event=stop_event)
        usage = getattr(response, "usage", None) or {}
        return LLMResponse(
            content=str(getattr(response, "content", "") or ""),
            request_id=getattr(response, "request_id", None),
            model=getattr(response, "model", None),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


@dataclass
class COAPAgentRuntime:
    """一个环境 Agent 拥有的 COAP 组件。"""

    agent: Any
    perception: Any
    verbalizer: Any
    buffer: SingleSlotOverwriteBuffer
    interrupt_event: threading.Event
    coordinator: COAPDecisionCoordinator
    observation_worker: ObservationWorker
    acting_worker: COAPActingWorker
    planning_worker: COAPPlanningWorker
    cache_planning_worker: COAPCachePlanningWorker

    @property
    def workers(self) -> tuple[Any, ...]:
        return (
            self.observation_worker,
            self.acting_worker,
            self.planning_worker,
            self.cache_planning_worker,
        )


class COAPRunner:
    """装配并运行一组 COAP 并行线路（缓存管理/执行/定时规划/缓存规划）。"""

    def __init__(
        self,
        *,
        session: TaskEnvironmentSession,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: COAPConfig | None = None,
        registry_path: str | None = None,
        runtime_closer: RuntimeCloser | None = None,
        observation_components_factory: ObservationComponentsFactory | None = None,
        cache_source: CacheSourceBundle | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        episode_recorder: EpisodeRecorder | None = None,
    ) -> None:
        self.session = session
        self._block_entity_provider = (
            RconBlockEntityProvider(session.server)
            if session.spec.category == "trade" and hasattr(session.server, "send_command")
            else None
        )
        self.llm_client = llm_client
        self.config = apply_task_runtime_overrides(
            config or COAPConfig(), session.spec
        )
        self.memory = memory or (
            session.memory if isinstance(session.memory, TeamMemory) else TeamMemory()
        )
        self.session.memory = self.memory
        self._trade_recipe_history = create_trade_recipe_history(session.spec)
        self.registry_path = registry_path
        self._runtime_closer = runtime_closer
        self._observation_components_factory = (
            observation_components_factory or self._default_observation_components
        )
        self.cache_source = cache_source
        self._monotonic = monotonic
        self._wall_time = wall_time
        self.episode_recorder = episode_recorder
        self._execution_table: ActionExecutionTable | None = None
        self._call_file_resolver = None
        self._cache_change_sequence = 0
        self._cache_changes_path: Path | None = None
        if episode_recorder is not None:
            episode_path = Path(episode_recorder.path)
            self._execution_table = ActionExecutionTable(
                episode_path / "action_executions.csv"
            )
            self._call_file_resolver = make_call_file_resolver(
                episode_path / "llm" / "calls"
            )
            self._cache_changes_path = episode_path / "cache" / "changes.jsonl"
        server = getattr(session, "server", None)
        self._broadcast_dispatcher = (
            AsyncBroadcastDispatcher(
                server,
                event_sink=self._record_event,
                name="coap-broadcast",
            )
            if server is not None
            else None
        )

        self._instructions = SimpleInstructionRegistry()
        self._llm_port = _LLMClientPortAdapter(llm_client)
        self.prompt_builder = COAPPromptBuilder(
            language=self.config.language,
            action_ready_keyword=self.config.action_ready_keyword,
            candidate_policy=self.config.candidate_policy,
        )
        boot_parser = CacheBootParser()
        content_parser = CacheContentParser()
        initializer = ModelCacheInitializer(
            self._llm_port, self.prompt_builder, boot_parser
        )
        content_generator = ModelCacheContentGenerator(
            self._llm_port, self.prompt_builder, content_parser
        )
        self._content_generator = (
            content_generator
            if self.config.cache_reasoning_optimization_enabled
            else None
        )
        self.cache_service = CacheService(
            InMemoryCacheRepository(),
            boot_config=self.config.cache_boot,
            representation_config=self.config.cache_representation,
            retrieval_config=self.config.cache_retrieval,
            buffer_config=self.config.cache_buffer,
            score_config=self.config.cache_score,
            sharing_mode=self.config.cache_sharing,
            clock=None,
            initializer=initializer,
            content_generator=self._content_generator,
        )
        self.score_updater = ScoreUpdater(
            self.cache_service.repository,
            self.config.cache_score,
        )
        self.cache_service.score_updater = self.score_updater
        self.consistency = ImportantParameterActionConsistency(
            self.config.important_parameters,
            self.cache_service.action_canonicalizer,
        )

        self._lock = threading.RLock()
        self._fatal_event = threading.Event()
        self._stop_event = threading.Event()
        self._started = False
        self._closed = False
        self._closing = False
        self._runtimes: dict[str, COAPAgentRuntime] = {}
        self._cache_worker: COAPCacheWorker | None = None
        self._system_message_worker: SystemMessageWorker | None = None
        self._worker_errors: list[tuple[str, str]] = []
        self._latest_outcome: dict[str, Any] | None = None
        self._latest_metrics: dict[str, Any] = {}
        self._result = COAPRunResult()
        self.liveness_gate = EntityLivenessGate(
            self.session.agents,
            event_sink=self._record_event,
        )
        self._budget = RunBudget(
            team_cap=self.config.max_llm_calls,
            per_agent_cap=self.config.max_llm_calls_per_agent,
        )

    @classmethod
    def from_bundle(
        cls,
        bundle: TaskEnvironmentBundle,
        *,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: COAPConfig | None = None,
        output: Callable[[str], None] = print,
        observation_components_factory: ObservationComponentsFactory | None = None,
        episode_recorder: EpisodeRecorder | None = None,
        cache_source: CacheSourceBundle | None = None,
    ) -> "COAPRunner":
        active_config = config or COAPConfig()
        active_memory = memory or TeamMemory(
            default_chat_window=(
                active_config.chat_window or DEFAULT_TEAM_CHAT_WINDOW
            )
        )
        session = TaskEnvironmentSession(
            spec=bundle.spec,
            manager=bundle.manager,
            server=bundle.server,
            agents=bundle.agents,
            world_workspace=bundle.world_workspace,
            memory=active_memory,
            defer_task_timing=True,
            output=output,
        )
        return cls(
            session=session,
            llm_client=llm_client,
            memory=active_memory,
            config=active_config,
            registry_path=str(bundle.registry_file),
            runtime_closer=bundle.close_runtime,
            observation_components_factory=observation_components_factory,
            cache_source=cache_source,
            episode_recorder=episode_recorder,
        )

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("COAPRunner can only start once")
            if self._closed:
                raise RuntimeError("COAPRunner is already closed")
            self._started = True

        try:
            self._record_event("runner.starting")
            self.session.start()
            if self._broadcast_dispatcher is not None:
                self._broadcast_dispatcher.start()
            start_task_timing = getattr(self.session, "start_task_timing", None)
            if callable(start_task_timing):
                start_task_timing()
            with self._lock:
                self._result.started_at = self._wall_time()
            self._record_event(
                "session.started",
                agents=[str(agent.username) for agent in self.session.agents],
            )
            manager = getattr(self.session, "manager", None)
            metrics = getattr(manager, "metrics", None)
            context = getattr(self.session, "context", None)
            if callable(metrics) and context is not None:
                self._latest_metrics = dict(metrics(context))
            self._start_system_message_worker()
            self._boot_cache()
            self._build_agent_runtimes()

            for runtime in self._runtimes.values():
                runtime.observation_worker.start()
            self._wait_for_initial_observations()
            for runtime in self._runtimes.values():
                runtime.acting_worker.start()
            for runtime in self._runtimes.values():
                runtime.planning_worker.start()
            if self.config.cache_planning_enabled:
                for runtime in self._runtimes.values():
                    runtime.cache_planning_worker.start()
            self._cache_worker.start()
            self._record_event("runner.started")
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                with self._lock:
                    self._result.interrupted = True
                    if self._result.stop_reason is None:
                        self._result.stop_reason = "keyboard_interrupt"
            else:
                self._set_runner_error(f"startup failed: {exc}")
            self.close()
            raise

    def wait(self) -> COAPRunResult:
        with self._lock:
            if not self._started:
                raise RuntimeError("COAPRunner has not started")
            if self._result.finished_at is not None:
                return self._result
        try:
            while not self._stop_event.is_set():
                if self._fatal_event.is_set():
                    self._set_runner_error("a COAP worker reached a fatal error")
                    self._set_stop_reason("worker_fatal")
                    break
                try:
                    poll = self.session.poll()
                except Exception as exc:  # noqa: BLE001
                    self._set_runner_error(f"task polling failed: {exc}")
                    break
                with self._lock:
                    self._latest_outcome = poll.outcome.to_dict()
                    self._latest_metrics = dict(poll.metrics)
                    self._result.final_metrics = dict(poll.metrics)
                    self.liveness_gate.update_from_task_state(
                        outcome=poll.outcome,
                        metrics=poll.metrics,
                    )
                    if poll.outcome.status != TaskStatus.RUNNING:
                        self._result.outcome = poll.outcome
                        if self._result.finished_at is None:
                            self._result.finished_at = self._wall_time()
                        self._set_stop_reason("task_outcome")
                if poll.outcome.status != TaskStatus.RUNNING:
                    break
                now = self._wall_time()
                max_run_reached = (
                    self.config.max_run_seconds is not None
                    and now - (self._result.started_at or 0.0)
                    >= self.config.max_run_seconds
                )
                if max_run_reached:
                    with self._lock:
                        if self._result.finished_at is None:
                            self._result.finished_at = now
                    self._set_stop_reason("max_run_seconds")
                    break
                self._stop_event.wait(self.config.task_poll_interval)
        except KeyboardInterrupt:
            with self._lock:
                self._result.interrupted = True
                if self._result.stop_reason is None:
                    self._result.stop_reason = "keyboard_interrupt"
            self._record_event("runner.interrupted")
        finally:
            self.close()
        return self._result

    def status(self) -> dict[str, Any]:
        with self._lock:
            runtimes = list(self._runtimes.values())
            return {
                "started": self._started,
                "closed": self._closed,
                "outcome": dict(self._latest_outcome) if self._latest_outcome else None,
                "metrics": dict(self._latest_metrics),
                "runner_error": self._result.runner_error,
                "worker_errors": [list(item) for item in self._worker_errors],
                "planned_count": sum(
                    runtime.planning_worker.planned_count
                    + runtime.cache_planning_worker.planned_count
                    for runtime in runtimes
                ),
                "executed_count": sum(
                    runtime.acting_worker.executed_count for runtime in runtimes
                ),
                "cache_stats": self._cache_stats(),
                "agents": {
                    str(runtime.agent.username): {
                        worker.name: {
                            "running": worker.is_running,
                            "iterations": worker.iteration_count,
                            "last_error": (
                                str(worker.last_error) if worker.last_error else None
                            ),
                        }
                        for worker in runtime.workers
                    }
                    for runtime in runtimes
                },
            }

    def run(self) -> COAPRunResult:
        self.start()
        return self.wait()

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            runtimes = list(self._runtimes.values())
        close_started = time.monotonic()
        self._stop_event.set()
        cleanup_errors: list[tuple[str, str]] = []
        self._record_event(
            "runner.stopping",
            strict=False,
            errors=cleanup_errors,
            worker_stop_timeout=self.config.worker_stop_timeout,
        )
        system_message_worker = self._system_message_worker
        self._system_message_worker = None
        workers_to_stop: list[Any] = []
        if system_message_worker is not None:
            workers_to_stop.append(system_message_worker)
        for runtime in runtimes:
            workers_to_stop.extend(runtime.workers)
        if self._cache_worker is not None:
            workers_to_stop.append(self._cache_worker)
        pending_workers = self._stop_workers(
            workers_to_stop,
            cleanup_errors,
            timeout=self.config.worker_stop_timeout,
            join=False,
        )
        self._cancel_llm_requests(cleanup_errors)
        self._join_workers(
            pending_workers,
            cleanup_errors,
            timeout=self.config.worker_stop_timeout,
        )
        for runtime in runtimes:
            teardown_error = runtime.acting_worker.last_error
            if teardown_error is not None:
                cleanup_errors.append(
                    (runtime.acting_worker.name, str(teardown_error))
                )
        if self._cache_worker is not None and not self._cache_worker.is_running:
            try:
                self._cache_worker.flush_pending_existing_statistics()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append((self._cache_worker.name, str(exc)))
        if self._broadcast_dispatcher is not None:
            self._broadcast_dispatcher.close(timeout=self.config.worker_stop_timeout)
        try:
            session_errors = self.session.close()
            cleanup_errors.extend(session_errors)
            self._record_event("cleanup.session", strict=False, errors=cleanup_errors)
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(("task_environment", str(exc)))
        if self._runtime_closer is not None:
            try:
                self._runtime_closer()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(("bundle_runtime", str(exc)))
        with self._lock:
            self._result.worker_errors = list(self._worker_errors)
            self._result.cleanup_errors.extend(cleanup_errors)
            self._result.planned_count = sum(
                runtime.planning_worker.planned_count
                + runtime.cache_planning_worker.planned_count
                for runtime in runtimes
            )
            self._result.executed_count = sum(
                runtime.acting_worker.executed_count for runtime in runtimes
            )
            self._result.cache_stats = self._cache_stats()
            if self._result.finished_at is None:
                self._result.finished_at = self._wall_time()
            self._closing = False
            self._closed = True
        self._export_cache_snapshot()
        if self._execution_table is not None:
            try:
                self._execution_table.close()
            except Exception:  # noqa: BLE001
                pass
        self._record_event(
            "runner.closed",
            strict=False,
            errors=cleanup_errors,
            cleanup_elapsed_seconds=time.monotonic() - close_started,
            result=self._result.to_dict(),
        )
        if self.episode_recorder is not None:
            try:
                self.episode_recorder.finalize(
                    status=(
                        "interrupted"
                        if self._result.interrupted
                        else "completed"
                        if self._result.exit_code == 0
                        else "failed"
                    ),
                    result=self._result,
                    memory=self.memory,
                    error=self._result.runner_error,
                )
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(("recording", str(exc)))

    def _boot_cache(self) -> None:
        spec = self.session.spec
        action_space = self._action_space()
        task_context = TaskContext(
            task_id=spec.task_id,
            goal=task_description_text(spec),
            action_space=action_space,
        )
        if self._content_generator is not None:
            self._content_generator.configure_context(
                task_context,
                action_space=action_space,
            )
        old_entries = (
            self.cache_source.shared_entries
            if self.cache_source is not None
            else None
        )
        result = self.cache_service.boot.boot(
            task_context,
            action_space,
            task_scope=spec.task_id,
            old_entries=old_entries,
        )
        changes_file = self._record_cache_changes(result.changes, ensure_file=True)
        event_data: dict[str, Any] = {
            "mode": result.mode,
            "created": result.created_count,
            "reused": result.reused_count,
            "filtered": result.filtered_count,
            "llm_calls": result.llm_call_count,
            "change_count": len(result.changes),
        }
        if changes_file is not None:
            event_data["changes_file"] = changes_file
        self._record_event("cache.booted", **event_data)

    def _action_space(self) -> tuple[ActionSpec, ...]:
        registry = self._first_registry()
        enable_chat = self.config.enable_chat_actions
        specs: list[ActionSpec] = []
        for skill_name, manifest in registry.manifests.items():
            for command_name in manifest.commands:
                action = f"{skill_name}.{command_name}"
                if not registry.is_allowed(action):
                    continue
                if not enable_chat and action.startswith("chat."):
                    continue
                _, spec = registry.resolve(action)
                instruction = self._instructions.instruction_for_action(action)
                metadata = {}
                if instruction is not None:
                    metadata["cache_boot_action_name"] = instruction.alias
                specs.append(
                    ActionSpec(
                        name=action,
                        description=str(spec.raw.get("llm_description") or ""),
                        parameter_schema=dict(
                            spec.raw.get("params_schema") or {}
                        ),
                        metadata=metadata,
                    )
                )
        return tuple(sorted(specs, key=lambda item: item.name))

    def _build_agent_runtimes(self) -> None:
        agents = list(self.session.agents)
        names = [str(agent.username) for agent in agents]
        if len(set(names)) != len(names):
            raise ValueError("COAP agent usernames must be unique")
        for agent in agents:
            agent_id = str(agent.username)
            registry = agent.action_planner.registry
            rule_factory = self._rule_factory()
            perception, verbalizer = self._observation_components_factory(
                agent,
                rule_factory,
            )
            observation = ObservationWorker(
                agent_id=agent_id,
                perception=perception,
                verbalizer=verbalizer,
                memory=self.memory,
                config=self.config,
                rule_factory=rule_factory,
                liveness_gate=self.liveness_gate,
                event_sink=self._record_event,
            )
            codec = COAPDecisionCodec(
                registry,
                self._instructions,
                enable_chat_actions=self.config.enable_chat_actions,
                action_ready_keyword=self.config.action_ready_keyword,
                candidate_policy=self.config.candidate_policy,
            )
            buffer = SingleSlotOverwriteBuffer()
            interrupt_event = threading.Event()
            coordinator = COAPDecisionCoordinator(
                agent_id=agent_id,
                cache_service=self.cache_service,
                task_scope=self.session.spec.task_id,
            )
            broadcast = self._broadcast
            acting = COAPActingWorker(
                agent=agent,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=self.memory,
                config=self.config,
                coordinator=coordinator,
                cache_service=self.cache_service,
                score_updater=self.score_updater,
                consistency=self.consistency,
                instructions=self._instructions,
                broadcast=broadcast,
                event_sink=self._record_event,
                execution_table=self._execution_table,
                call_file_resolver=self._call_file_resolver,
                task_category=self.session.spec.category,
                trade_action_observer=(
                    self._trade_recipe_history.record_action_result
                    if self._trade_recipe_history is not None
                    else None
                ),
                liveness_gate=self.liveness_gate,
            )
            teammates = [name for name in names if name != agent_id]
            planning = COAPPlanningWorker(
                agent=agent,
                teammates=teammates,
                llm_client=self.llm_client,
                prompt_builder=self.prompt_builder,
                codec=codec,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=self.memory,
                config=self.config,
                coordinator=coordinator,
                registry=registry,
                spec=self.session.spec,
                instructions=self._instructions,
                budget=self._budget,
                broadcast=broadcast,
                event_sink=self._record_event,
                trade_recipe_history=self._trade_recipe_history,
                liveness_gate=self.liveness_gate,
            )
            cache_planning = COAPCachePlanningWorker(
                agent=agent,
                llm_client=self.llm_client,
                prompt_builder=self.prompt_builder,
                codec=codec,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=self.memory,
                config=self.config,
                coordinator=coordinator,
                registry=registry,
                spec=self.session.spec,
                instructions=self._instructions,
                budget=self._budget,
                broadcast=broadcast,
                event_sink=self._record_event,
                trade_recipe_history=self._trade_recipe_history,
                liveness_gate=self.liveness_gate,
            )
            for worker in (acting, planning, cache_planning):
                worker.on_error = self._on_worker_error
            observation.on_error = self._on_worker_error
            self._runtimes[agent_id] = COAPAgentRuntime(
                agent=agent,
                perception=perception,
                verbalizer=verbalizer,
                buffer=buffer,
                interrupt_event=interrupt_event,
                coordinator=coordinator,
                observation_worker=observation,
                acting_worker=acting,
                planning_worker=planning,
                cache_planning_worker=cache_planning,
            )
        self._cache_worker = COAPCacheWorker(
            cache_service=self.cache_service,
            score_updater=self.score_updater,
            task_scope=self.session.spec.task_id,
            config=self.config,
            liveness_gate=self.liveness_gate,
            event_sink=self._record_event,
            change_sink=self._record_cache_changes,
        )
        self._cache_worker._agent_ids_value = tuple(names)
        self._cache_worker.on_error = self._on_worker_error

    def _wait_for_initial_observations(self) -> None:
        deadline = self._monotonic() + self.config.initial_observation_timeout
        pending = set(self._runtimes)
        while pending:
            if self._fatal_event.is_set():
                raise RuntimeError("observation worker failed before initial snapshot")
            pending = {
                agent_id
                for agent_id in pending
                if not self._runtimes[
                    agent_id
                ].observation_worker.first_observation.is_set()
            }
            if not pending:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "initial observation timed out for: " + ", ".join(sorted(pending))
                )
            self._fatal_event.wait(min(0.02, remaining))

    def _start_system_message_worker(self) -> None:
        if not self.config.system_messages_enabled:
            return
        manager = getattr(self.session, "manager", None)
        if manager is None:
            self._record_event("system_message.skipped", reason="missing_manager")
            return
        worker = SystemMessageWorker(
            name="coap-system-messages",
            manager=manager,
            context_provider=lambda: self.session.context,
            memory=self.memory,
            interval=self.config.system_message_interval,
            broadcast=self._broadcast,
            event_sink=self._record_event,
        )
        worker.on_error = self._on_worker_error
        self._system_message_worker = worker
        worker.start()
        self._record_event("worker.started", worker=worker.name)

    def _stop_system_message_worker(
        self,
        cleanup_errors: list[tuple[str, str]],
        *,
        deadline: float | None = None,
    ) -> None:
        worker = self._system_message_worker
        if worker is None:
            return
        self._stop_workers(
            [worker],
            cleanup_errors,
            deadline=(
                deadline
                if deadline is not None
                else time.monotonic() + self.config.worker_stop_timeout
            ),
        )
        self._system_message_worker = None

    def _stop_workers(
        self,
        workers: Sequence[Any],
        cleanup_errors: list[tuple[str, str]],
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        join: bool = True,
    ) -> list[Any]:
        pending: list[Any] = []
        seen: set[int] = set()
        for worker in workers:
            if id(worker) in seen:
                continue
            seen.add(id(worker))
            try:
                worker.stop(reason="runner_stopped", timeout=0.0)
                pending.append(worker)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append((worker.name, str(exc)))

        if not join:
            return pending
        self._join_workers(
            pending,
            cleanup_errors,
            timeout=timeout,
            deadline=deadline,
        )
        return pending

    def _join_workers(
        self,
        workers: Sequence[Any],
        cleanup_errors: list[tuple[str, str]],
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        if timeout is None:
            timeout = max(0.0, (deadline or time.monotonic()) - time.monotonic())
        for worker in workers:
            try:
                worker.join(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append((worker.name, str(exc)))

        for worker in workers:
            if worker.is_running:
                cleanup_errors.append(
                    (worker.name, "did not stop within worker_stop_timeout")
                )

    def _cancel_llm_requests(
        self,
        cleanup_errors: list[tuple[str, str]],
    ) -> None:
        cancel = getattr(self.llm_client, "cancel_active_requests", None)
        if not callable(cancel):
            return
        try:
            cancelled = cancel(reason="runner_stopped")
        except TypeError:
            cancelled = cancel()
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(("llm_client", str(exc)))
            return
        self._record_event(
            "cleanup.llm_requests_cancelled",
            strict=False,
            count=cancelled if isinstance(cancelled, int) else None,
        )

    def _first_registry(self) -> Any:
        if not self.session.agents:
            raise RuntimeError("COAP 环境没有 Agent")
        return self.session.agents[0].action_planner.registry

    def _default_observation_components(
        self,
        agent: Any,
        rule_factory: Callable[[], Any],
    ) -> tuple[Any, Any]:
        if self.session.spec.category in TASK_PERCEPTION_PROFILES:
            verbalizer = task_verbalizer(
                self.session.spec.category,
                self.config.language,
            )
        else:
            verbalizer = world_verbalizer(self.config.language)
        return (
            BasePerception(
                agent,
                radius=self.session.spec.observation_range,
                rule=rule_factory(),
            ),
            verbalizer,
        )

    def _rule_factory(self) -> Callable[[], Any]:
        def build_rule() -> Any:
            context = self.session.context
            elapsed = context.elapsed_seconds() if context is not None else 0.0
            with self._lock:
                metrics = dict(self._latest_metrics)
            if self.session.spec.category in TASK_PERCEPTION_PROFILES:
                return ChainRule(
                    [
                        world_snapshot(provider=self._block_entity_provider),
                        task_perception(
                            self.session.spec,
                            metrics=metrics,
                            elapsed_seconds=elapsed,
                            registry_path=self.registry_path,
                        ),
                    ]
                )
            return world_snapshot(provider=self._block_entity_provider)

        return build_rule

    def _cache_stats(self) -> dict[str, Any]:
        try:
            entries = self.cache_service.repository.list_entries()
            statistics = self.score_updater.episode_statistics()
            service_statistics = self.cache_service.episode_statistics()
            content_updater = self.cache_service.content_updater
            if content_updater is not None:
                content_statistics = content_updater.episode_statistics()
                for key in ("attempt_count", "success_count"):
                    statistics[key] += content_statistics[key]
            for key in ("attempt_count", "success_count"):
                statistics[key] += service_statistics[key]
            cache_workers = [
                runtime.cache_planning_worker for runtime in self._runtimes.values()
            ]
            return {
                "entry_count": len(entries),
                "hit_count": statistics["hit_count"],
                "attempt_count": statistics["attempt_count"],
                "success_count": statistics["success_count"],
                "buffer_size": self.cache_service.buffer.size(),
                "update_count": (
                    self._cache_worker.update_count
                    if self._cache_worker is not None
                    else 0
                ),
                "retrieval_count": sum(
                    worker.retrieval_count for worker in cache_workers
                ),
                "cache_miss_count": sum(
                    worker.cache_miss_count for worker in cache_workers
                ),
                "planning_call_count": sum(
                    worker.planning_call_count for worker in cache_workers
                ),
                "planning_success_count": sum(
                    worker.planning_success_count for worker in cache_workers
                ),
                "planning_failure_count": sum(
                    worker.planning_failure_count for worker in cache_workers
                ),
                "budget_exhausted_count": sum(
                    worker.budget_exhausted_count for worker in cache_workers
                ),
            }
        except Exception:  # noqa: BLE001
            return {}

    def _export_cache_snapshot(self) -> None:
        """共享缓存模式：把缓存条目快照写入 episode 的 cache/shared.json。

        非共享（per-agent）模式沿用当前 episode 结构，不生成缓存文件。
        """
        if self.episode_recorder is None:
            return
        if self.config.cache_sharing is not CacheSharingMode.SHARED:
            return
        try:
            entries = self.cache_service.repository.list_entries()
            cache_dir = Path(self.episode_recorder.path) / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "kind": "coap_shared_cache",
                "task_id": self.session.spec.task_id,
                "sharing": "shared",
                "exported_at": self._wall_time(),
                "entry_count": len(entries),
                "entries": [_cache_entry_dict(entry) for entry in entries],
            }
            target = cache_dir / "shared.json"
            temporary = cache_dir / f".shared.json.{uuid4().hex}.tmp"
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temporary.replace(target)
        except Exception:  # noqa: BLE001
            return

    def _broadcast(self, message: str) -> None:
        if self._broadcast_dispatcher is not None:
            self._broadcast_dispatcher.enqueue(message)

    def _on_worker_error(self, exc: Exception, worker: Any) -> None:
        item = (worker.name, str(exc))
        with self._lock:
            if item not in self._worker_errors:
                self._worker_errors.append(item)
        self._fatal_event.set()
        self._record_event(
            "worker.failed",
            worker=worker.name,
            error=str(exc),
        )

    def _set_runner_error(self, message: str) -> None:
        with self._lock:
            if self._result.runner_error is None:
                self._result.runner_error = message

    def _set_stop_reason(self, reason: str) -> None:
        with self._lock:
            if self._result.stop_reason is None:
                self._result.stop_reason = reason

    def _record_event(self, event_type: str, **data: Any) -> None:
        if self.episode_recorder is None:
            return
        try:
            self.episode_recorder.event(event_type, **data)
        except Exception:  # noqa: BLE001
            return

    def _record_cache_changes(
        self,
        changes: Sequence[CacheEntryChange],
        *,
        ensure_file: bool = False,
    ) -> str | None:
        if self._cache_changes_path is None:
            return None
        try:
            self._cache_changes_path.parent.mkdir(parents=True, exist_ok=True)
            if not changes:
                if ensure_file:
                    self._cache_changes_path.touch(exist_ok=True)
                    return "cache/changes.jsonl"
                return None
            with self._cache_changes_path.open(
                "a", encoding="utf-8", newline="\n"
            ) as handle:
                for change in changes:
                    self._cache_change_sequence += 1
                    handle.write(
                        json.dumps(
                            self._cache_change_payload(change),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            return "cache/changes.jsonl"
        except Exception as exc:  # noqa: BLE001
            self._record_event("cache.change_record_failed", error=str(exc))
            return None

    def _cache_change_payload(self, change: CacheEntryChange) -> dict[str, Any]:
        entry = change.after or change.before
        return {
            "schema_version": 1,
            "sequence": self._cache_change_sequence,
            "created_at": self._wall_time(),
            "event": change.event,
            "entry_id": change.entry_id,
            "task_scope": None if entry is None else entry.task_scope,
            "owner_agent_id": None if entry is None else entry.owner_agent_id,
            "previous_action": (
                None if entry is None else _cache_action_dict(entry.previous_action)
            ),
            "previous_action_alias": (
                None if entry is None else self._cache_action_alias(entry.previous_action)
            ),
            "next_action": (
                None if entry is None else _cache_action_dict(entry.next_action)
            ),
            "next_action_alias": (
                None if entry is None else self._cache_action_alias(entry.next_action)
            ),
            "before": (
                None
                if change.before is None
                else self._cache_entry_audit_dict(change.before)
            ),
            "after": (
                None
                if change.after is None
                else self._cache_entry_audit_dict(change.after)
            ),
            "related_record_ids": list(change.related_record_ids),
            "llm_call_count": change.llm_call_count,
            "source": change.source,
        }

    def _cache_entry_audit_dict(self, entry: Any) -> dict[str, Any]:
        payload = _cache_entry_dict(entry)
        payload["previous_action_alias"] = self._cache_action_alias(
            entry.previous_action
        )
        payload["next_action_alias"] = self._cache_action_alias(entry.next_action)
        return payload

    def _cache_action_alias(self, action: Action) -> str:
        instruction = self._instructions.instruction_for_action(action.name)
        if instruction is not None:
            return instruction.alias
        metadata_alias = action.metadata.get("cache_boot_action_name")
        if isinstance(metadata_alias, str) and metadata_alias.strip():
            return metadata_alias.strip()
        return action.name


def _cache_action_dict(action: Action) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": action.name,
        "parameters": dict(action.parameters),
    }
    if action.agent_id is not None:
        payload["agent_id"] = action.agent_id
    if action.canonical_key is not None:
        payload["canonical_key"] = action.canonical_key
    if action.metadata:
        payload["metadata"] = dict(action.metadata)
    return payload


def _cache_entry_dict(entry: Any) -> dict[str, Any]:
    """缓存条目序列化为 JSON 友好的字典（供共享缓存快照使用）。"""
    return {
        "entry_id": entry.entry_id,
        "previous_action": _cache_action_dict(entry.previous_action),
        "next_action": _cache_action_dict(entry.next_action),
        "reason": entry.reason.text,
        "reason_metadata": dict(entry.reason.metadata),
        "score": entry.score,
        "source": entry.source.value,
        "hit_count": entry.hit_count,
        "attempt_count": entry.attempt_count,
        "success_count": entry.success_count,
        "delta_attempt_count": entry.delta_attempt_count,
        "delta_success_count": entry.delta_success_count,
        "delta_hit_count": entry.delta_hit_count,
        "task_scope": entry.task_scope,
        "owner_agent_id": entry.owner_agent_id,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "last_hit_at": entry.last_hit_at,
        "seed_entry_id": entry.seed_entry_id,
        "initialization_batch_id": entry.initialization_batch_id,
        "metadata": dict(entry.metadata),
    }


def load_shared_cache_snapshot(path: str | Path) -> CacheSourceBundle:
    """从 episode/cache/shared.json 恢复共享缓存源。"""
    source_path = Path(path)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"无法读取 COAP shared cache: {source_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"COAP shared cache 必须是 JSON 对象: {source_path}")
    if payload.get("kind") != "coap_shared_cache" or payload.get("sharing") != "shared":
        raise ValueError(f"不是 COAP shared cache 快照: {source_path}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"COAP shared cache entries 必须是列表: {source_path}")
    entries = tuple(
        _cache_entry_from_dict(item, index=index)
        for index, item in enumerate(raw_entries)
    )
    return CacheSourceBundle(
        layout="shared",
        shared_entries=entries,
        source_path=str(source_path),
        metadata={
            "task_id": payload.get("task_id"),
            "exported_at": payload.get("exported_at"),
            "entry_count": payload.get("entry_count"),
        },
    )


def _cache_entry_from_dict(value: Any, *, index: int) -> CacheEntry:
    if not isinstance(value, dict):
        raise ValueError(f"entries[{index}] 必须是对象")
    kwargs: dict[str, Any] = {
        "previous_action": _cache_action_from_dict(
            value.get("previous_action"), f"entries[{index}].previous_action"
        ),
        "next_action": _cache_action_from_dict(
            value.get("next_action"), f"entries[{index}].next_action"
        ),
        "reason": _cache_reason_from_dict(value),
        "score": float(value.get("score", 0.5)),
        "source": CacheSource(value.get("source", CacheSource.LLM.value)),
        "hit_count": _int_field(value, "hit_count", default=0),
        "attempt_count": _int_field(value, "attempt_count", default=0),
        "success_count": _int_field(value, "success_count", default=0),
        "delta_attempt_count": _int_field(
            value, "delta_attempt_count", default=0
        ),
        "delta_success_count": _int_field(
            value, "delta_success_count", default=0
        ),
        "delta_hit_count": _int_field(value, "delta_hit_count", default=0),
        "metadata": dict(value.get("metadata") or {}),
    }
    for name in (
        "entry_id",
        "task_scope",
        "owner_agent_id",
        "seed_entry_id",
        "initialization_batch_id",
    ):
        if value.get(name) is not None:
            kwargs[name] = str(value[name])
    for name in ("created_at", "updated_at", "last_hit_at"):
        if value.get(name) is not None:
            kwargs[name] = float(value[name])
    return CacheEntry(**kwargs)


def _cache_action_from_dict(value: Any, label: str) -> Action:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}.name 必须是非空字符串")
    parameters = value.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError(f"{label}.parameters 必须是对象")
    metadata = value.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{label}.metadata 必须是对象")
    return Action(
        name=name,
        parameters=dict(parameters),
        agent_id=_optional_str(value.get("agent_id")),
        canonical_key=_optional_str(value.get("canonical_key")),
        metadata=dict(metadata),
    )


def _cache_reason_from_dict(value: dict[str, Any]) -> CacheReason:
    reason = value.get("reason")
    if isinstance(reason, dict):
        text = reason.get("text")
        metadata = reason.get("metadata") or {}
    else:
        text = reason
        metadata = value.get("reason_metadata") or {}
    if not isinstance(text, str) or not text.strip():
        raise ValueError("reason 必须是非空字符串")
    if not isinstance(metadata, dict):
        raise ValueError("reason metadata 必须是对象")
    return CacheReason(text=text, metadata=dict(metadata))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_field(value: dict[str, Any], name: str, *, default: int) -> int:
    raw = value.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{name} 必须是整数")
    return raw


__all__ = ["COAPAgentRuntime", "COAPRunner", "load_shared_cache_snapshot"]
