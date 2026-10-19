"""Task-environment integrated runner for the PPA baseline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any

from src.agents.action import SimpleInstructionRegistry
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
from src.tasks.trade import create_trade_recipe_history
from src.launcher.broadcast_dispatcher import AsyncBroadcastDispatcher

from .budget import RunBudget
from .prompts import PPAPromptBuilder
from .schemas import PPAConfig, PPARunResult
from .text_protocol import (
    DecisionCodec,
    JsonDecisionCodec,
)
from .workers import ActingWorker, ObservationWorker, PlanningWorker, RuleFactory


ObservationComponentsFactory = Callable[[Any, RuleFactory], tuple[Any, Any]]
RuntimeCloser = Callable[[], None]


@dataclass
class PPAAgentRuntime:
    """All PPA components owned by one environment agent."""

    agent: Any
    perception: Any
    verbalizer: Any
    buffer: SingleSlotOverwriteBuffer
    interrupt_event: threading.Event
    observation_worker: ObservationWorker
    acting_worker: ActingWorker
    planning_worker: PlanningWorker

    @property
    def workers(self) -> tuple[Any, ...]:
        return (
            self.observation_worker,
            self.acting_worker,
            self.planning_worker,
        )


class PPARunner:
    """Assemble and run one text-based parallel planning/acting team."""

    def __init__(
        self,
        *,
        session: TaskEnvironmentSession,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: PPAConfig | None = None,
        codec: DecisionCodec | None = None,
        registry_path: str | None = None,
        runtime_closer: RuntimeCloser | None = None,
        observation_components_factory: ObservationComponentsFactory | None = None,
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
            config or PPAConfig(), session.spec
        )
        if codec is not None:
            self.codec = codec
            self._instructions = None
        elif self.config.simple_instructions:
            self._instructions = SimpleInstructionRegistry()
            self.codec = JsonDecisionCodec(instructions=self._instructions)
        else:
            self._instructions = None
            self.codec = JsonDecisionCodec()
        self.memory = memory or (
            session.memory if isinstance(session.memory, TeamMemory) else TeamMemory()
        )
        self.session.memory = self.memory
        self.registry_path = registry_path
        self._runtime_closer = runtime_closer
        self._observation_components_factory = (
            observation_components_factory or self._default_observation_components
        )
        self._monotonic = monotonic
        self._wall_time = wall_time
        self.episode_recorder = episode_recorder
        self._trade_recipe_history = create_trade_recipe_history(session.spec)
        self._execution_table: ActionExecutionTable | None = None
        self._call_file_resolver = None
        if episode_recorder is not None:
            episode_path = Path(episode_recorder.path)
            self._execution_table = ActionExecutionTable(
                episode_path / "action_executions.csv"
            )
            self._call_file_resolver = make_call_file_resolver(
                episode_path / "llm" / "calls"
            )
        self._budget = RunBudget(
            team_cap=self.config.max_llm_calls,
            per_agent_cap=self.config.max_llm_calls_per_agent,
        )
        self._run_started_at: float | None = None
        if episode_recorder is not None:
            bind_recorder = getattr(llm_client, "bind_call_recorder", None)
            if callable(bind_recorder):
                bind_recorder(episode_recorder.llm_calls)

        server = getattr(session, "server", None)
        self._broadcast_dispatcher = (
            AsyncBroadcastDispatcher(
                server,
                event_sink=self._record_event,
                name="ppa-broadcast",
            )
            if server is not None
            else None
        )

        self._lock = threading.RLock()
        self._fatal_event = threading.Event()
        self._stop_event = threading.Event()
        self._started = False
        self._closed = False
        self._closing = False
        self._runtimes: dict[str, PPAAgentRuntime] = {}
        self._system_message_worker: SystemMessageWorker | None = None
        self._worker_errors: list[tuple[str, str]] = []
        self._latest_outcome: dict[str, Any] | None = None
        self._latest_metrics: dict[str, Any] = {}
        self._result = PPARunResult()
        self.liveness_gate = EntityLivenessGate(
            self.session.agents,
            event_sink=self._record_event,
        )

    @classmethod
    def from_bundle(
        cls,
        bundle: TaskEnvironmentBundle,
        *,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: PPAConfig | None = None,
        codec: DecisionCodec | None = None,
        output: Callable[[str], None] = print,
        observation_components_factory: ObservationComponentsFactory | None = None,
        episode_recorder: EpisodeRecorder | None = None,
    ) -> "PPARunner":
        """Create a runner that owns the bundle's temporary runtime directory."""
        active_config = config or PPAConfig()
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
            output=output,
        )
        return cls(
            session=session,
            llm_client=llm_client,
            memory=active_memory,
            config=active_config,
            codec=codec,
            registry_path=str(bundle.registry_file),
            runtime_closer=bundle.close_runtime,
            observation_components_factory=observation_components_factory,
            episode_recorder=episode_recorder,
        )

    def start(self) -> None:
        """Start the environment and workers in the required dependency order."""
        with self._lock:
            if self._started:
                raise RuntimeError("PPARunner can only start once")
            if self._closed:
                raise RuntimeError("PPARunner is already closed")
            self._started = True

        try:
            self._record_event("runner.starting")
            self.session.memory = self.memory
            self.session.start()
            if self._broadcast_dispatcher is not None:
                self._broadcast_dispatcher.start()
            with self._lock:
                self._result.started_at = self._wall_time()
                self._run_started_at = self._monotonic()
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
            self._build_agent_runtimes()

            for runtime in self._runtimes.values():
                runtime.observation_worker.start()
                self._record_event(
                    "worker.started", worker=runtime.observation_worker.name
                )
            self._wait_for_initial_observations()
            self._record_event("observations.ready")

            for runtime in self._runtimes.values():
                runtime.acting_worker.start()
                self._record_event("worker.started", worker=runtime.acting_worker.name)
            self._wait_for_acting_workers()
            self._record_event("acting.ready")

            for runtime in self._runtimes.values():
                runtime.planning_worker.start()
                self._record_event(
                    "worker.started", worker=runtime.planning_worker.name
                )
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

    def wait(self) -> PPARunResult:
        """Poll the task until terminal state, worker failure, or interruption."""
        with self._lock:
            if not self._started:
                raise RuntimeError("PPARunner has not started")
            if self._result.finished_at is not None:
                return self._result

        try:
            while not self._stop_event.is_set():
                if self._fatal_event.is_set():
                    self._set_runner_error("a PPA worker reached a fatal error")
                    self._set_stop_reason("worker_fatal")
                    break
                if self._budget.exhausted:
                    self._set_stop_reason("max_llm_calls")
                    break
                try:
                    poll = self.session.poll()
                except Exception as exc:  # noqa: BLE001
                    self._set_runner_error(f"task polling failed: {exc}")
                    break
                with self._lock:
                    previous_outcome = self._latest_outcome
                    previous_metrics = self._latest_metrics
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
                if (
                    previous_outcome != self._latest_outcome
                    or previous_metrics != self._latest_metrics
                ):
                    self._record_event(
                        "task.updated",
                        outcome=self._latest_outcome,
                        metrics=self._latest_metrics,
                    )
                if poll.outcome.status != TaskStatus.RUNNING:
                    break
                now = self._monotonic()
                max_run_reached = (
                    self.config.max_run_seconds is not None
                    and now - (self._run_started_at or 0.0)
                    >= self.config.max_run_seconds
                )
                if max_run_reached:
                    with self._lock:
                        if self._result.finished_at is None:
                            self._result.finished_at = self._wall_time()
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
        """Return a thread-safe, non-polling snapshot of runner state."""
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
                    runtime.planning_worker.planned_count for runtime in runtimes
                ),
                "executed_count": sum(
                    runtime.acting_worker.executed_count for runtime in runtimes
                ),
                "agents": {
                    str(runtime.agent.username): {
                        worker.name: {
                            "running": worker.is_running,
                            "ready": worker.ready,
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

    def close(self) -> None:
        """Stop workers and environment in reverse startup order; idempotent."""
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            runtimes = list(self._runtimes.values())
        self._stop_event.set()
        cleanup_errors: list[tuple[str, str]] = []
        self._record_event("runner.stopping", strict=False, errors=cleanup_errors)

        self._stop_system_message_worker(cleanup_errors)
        self._stop_workers(
            [runtime.planning_worker for runtime in runtimes],
            cleanup_errors,
        )
        self._stop_workers(
            [runtime.acting_worker for runtime in runtimes],
            cleanup_errors,
        )
        for runtime in runtimes:
            teardown_error = runtime.acting_worker.teardown_error
            if teardown_error:
                cleanup_errors.append((runtime.acting_worker.name, teardown_error))
            self._record_event(
                "cleanup.acting_teardown",
                strict=False,
                errors=cleanup_errors,
                agent_id=str(runtime.agent.username),
                ok=teardown_error is None,
                error=teardown_error,
            )
        self._stop_workers(
            [runtime.observation_worker for runtime in runtimes],
            cleanup_errors,
        )

        if self._broadcast_dispatcher is not None:
            self._broadcast_dispatcher.close(timeout=self.config.worker_stop_timeout)

        try:
            session_errors = self.session.close()
            cleanup_errors.extend(session_errors)
            self._record_event(
                "cleanup.session",
                strict=False,
                errors=cleanup_errors,
                ok=not session_errors,
                error="; ".join(str(item) for item in session_errors) or None,
            )
            self._record_event("session.closed", strict=False, errors=cleanup_errors)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            cleanup_errors.append(("task_environment", message))
            self._record_event(
                "cleanup.session",
                strict=False,
                errors=cleanup_errors,
                ok=False,
                error=message,
            )
        if self._runtime_closer is not None:
            try:
                self._runtime_closer()
                self._record_event(
                    "cleanup.runtime",
                    strict=False,
                    errors=cleanup_errors,
                    ok=True,
                    error=None,
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                cleanup_errors.append(("bundle_runtime", message))
                self._record_event(
                    "cleanup.runtime",
                    strict=False,
                    errors=cleanup_errors,
                    ok=False,
                    error=message,
                )

        with self._lock:
            self._result.worker_errors = list(self._worker_errors)
            self._result.cleanup_errors.extend(cleanup_errors)
            self._result.planned_count = sum(
                runtime.planning_worker.planned_count for runtime in runtimes
            )
            self._result.executed_count = sum(
                runtime.acting_worker.executed_count for runtime in runtimes
            )
            if self._result.finished_at is None:
                self._result.finished_at = self._wall_time()
            self._closing = False
            self._closed = True
        if self._execution_table is not None:
            try:
                self._execution_table.close()
            except Exception:  # noqa: BLE001
                pass
        self._record_event(
            "runner.closed",
            strict=False,
            errors=cleanup_errors,
            result=self._result.to_dict(),
        )
        if cleanup_errors:
            with self._lock:
                for item in cleanup_errors:
                    if item not in self._result.cleanup_errors:
                        self._result.cleanup_errors.append(item)
        if self.episode_recorder is not None:
            episode_status = (
                "interrupted"
                if self._result.interrupted
                else "completed" if self._result.exit_code == 0 else "failed"
            )
            try:
                self.episode_recorder.finalize(
                    status=episode_status,
                    result=self._result,
                    memory=self.memory,
                    error=self._result.runner_error,
                )
            except Exception as exc:  # noqa: BLE001
                item = ("recording", str(exc))
                with self._lock:
                    if item not in self._result.cleanup_errors:
                        self._result.cleanup_errors.append(item)

    def run(self) -> PPARunResult:
        """Start, wait, and clean up one complete PPA invocation."""
        self.start()
        return self.wait()

    def _build_agent_runtimes(self) -> None:
        agents = list(self.session.agents)
        names = [str(agent.username) for agent in agents]
        if len(set(names)) != len(names):
            raise ValueError("PPA agent usernames must be unique")
        prompt_builder = PPAPromptBuilder(
            spec=self.session.spec,
            memory=self.memory,
            codec=self.codec,
            config=self.config,
            instructions=self._instructions,
            trade_recipe_history=self._trade_recipe_history,
        )
        for agent in agents:
            agent_id = str(agent.username)
            rule_factory = self._rule_factory()
            perception, verbalizer = self._observation_components_factory(
                agent,
                rule_factory,
            )
            buffer = SingleSlotOverwriteBuffer()
            interrupt_event = threading.Event()
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
            acting = ActingWorker(
                agent=agent,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=self.memory,
                config=self.config,
                instructions=self._instructions,
                broadcast=self._broadcast,
                broadcast_results=self.config.broadcast_results,
                liveness_gate=self.liveness_gate,
                event_sink=self._record_event,
                execution_table=self._execution_table,
                call_file_resolver=self._call_file_resolver,
                task_category=self.session.spec.category,
                trade_action_observer=(
                    self._trade_recipe_history.record_action_result
                    if self._trade_recipe_history is not None
                    else None
                ),
            )
            planning = PlanningWorker(
                agent=agent,
                teammates=[name for name in names if name != agent_id],
                llm_client=self.llm_client,
                prompt_builder=prompt_builder,
                codec=self.codec,
                buffer=buffer,
                interrupt_event=interrupt_event,
                memory=self.memory,
                config=self.config,
                budget=self._budget,
                liveness_gate=self.liveness_gate,
                event_sink=self._record_event,
            )
            for worker in (observation, acting, planning):
                worker.on_error = self._on_worker_error
            self._runtimes[agent_id] = PPAAgentRuntime(
                agent=agent,
                perception=perception,
                verbalizer=verbalizer,
                buffer=buffer,
                interrupt_event=interrupt_event,
                observation_worker=observation,
                acting_worker=acting,
                planning_worker=planning,
            )

    def _rule_factory(self) -> RuleFactory:
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

    def _default_observation_components(
        self,
        agent: Any,
        rule_factory: RuleFactory,
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

    def _wait_for_initial_observations(self) -> None:
        deadline = self._monotonic() + self.config.initial_observation_timeout
        pending = set(self._runtimes)
        while pending:
            if self._fatal_event.is_set():
                raise RuntimeError("observation worker failed before initial snapshot")
            pending = {
                agent_id
                for agent_id in pending
                if not self._runtimes[agent_id].observation_worker.first_observation.is_set()
            }
            if not pending:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "initial observation timed out for: " + ", ".join(sorted(pending))
                )
            self._fatal_event.wait(min(0.02, remaining))

    def _wait_for_acting_workers(self) -> None:
        deadline = self._monotonic() + self.config.worker_stop_timeout
        for runtime in self._runtimes.values():
            remaining = max(0.0, deadline - self._monotonic())
            if not runtime.acting_worker.wait_ready(remaining):
                raise TimeoutError(f"acting worker not ready: {runtime.agent.username}")
            if runtime.acting_worker.last_error is not None:
                raise RuntimeError(
                    f"acting worker failed: {runtime.acting_worker.last_error}"
                )

    def _start_system_message_worker(self) -> None:
        if not self.config.system_messages_enabled:
            return
        manager = getattr(self.session, "manager", None)
        if manager is None:
            self._record_event(
                "system_message.skipped",
                reason="missing_manager",
            )
            return
        worker = SystemMessageWorker(
            name="ppa-system-messages",
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
    ) -> None:
        worker = self._system_message_worker
        if worker is None:
            return
        self._stop_workers([worker], cleanup_errors)
        self._system_message_worker = None

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

    def _stop_workers(
        self,
        workers: list[Any],
        cleanup_errors: list[tuple[str, str]],
    ) -> None:
        pending: list[Any] = []
        for worker in workers:
            try:
                worker.stop(reason="runner_stopped", timeout=0.0)
                pending.append(worker)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                cleanup_errors.append((worker.name, message))
                self._record_event(
                    "cleanup.worker_stopped",
                    strict=False,
                    errors=cleanup_errors,
                    worker=worker.name,
                    ok=False,
                    error=message,
                )
        for worker in pending:
            worker.join(timeout=self.config.worker_stop_timeout)
            error = (
                f"did not stop within {self.config.worker_stop_timeout}s"
                if worker.is_running
                else None
            )
            if error:
                cleanup_errors.append((worker.name, error))
            self._record_event(
                "cleanup.worker_stopped",
                strict=False,
                errors=cleanup_errors,
                worker=worker.name,
                ok=error is None,
                error=error,
            )

    def _set_runner_error(self, message: str) -> None:
        with self._lock:
            if self._result.runner_error is None:
                self._result.runner_error = message

    def _set_stop_reason(self, reason: str) -> None:
        with self._lock:
            if self._result.stop_reason is None:
                self._result.stop_reason = reason

    def _broadcast(self, message: str) -> None:
        """Queue a public result message without blocking the worker."""
        if self._broadcast_dispatcher is not None:
            self._broadcast_dispatcher.enqueue(message)

    def _record_event(
        self,
        event_type: str,
        *,
        strict: bool = True,
        errors: list[tuple[str, str]] | None = None,
        **data: Any,
    ) -> None:
        if self.episode_recorder is None:
            return
        try:
            self.episode_recorder.event(event_type, **data)
        except Exception as exc:
            if strict:
                raise
            if errors is not None:
                errors.append(("recording", str(exc)))


__all__ = ["PPARunner"]
