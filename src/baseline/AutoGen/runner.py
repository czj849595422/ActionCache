"""Runner for the AutoGen-style hierarchical A2A baseline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from src.baseline.BasicAsync.runner import (
    BasicAsyncRunner,
    ObservationComponentsFactory,
    RuntimeCloser,
)
from src.baseline.BasicAsync.workers import BasicActingWorker
from src.baseline.Parallelized.workers import ObservationWorker
from src.baseline.Parallelized.budget import RunBudget
from src.agents.action import SimpleInstructionRegistry
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory
from src.recording import EpisodeRecorder
from src.recording.table import ActionExecutionTable, make_call_file_resolver
from src.runtime import SingleSlotOverwriteBuffer
from src.tasks import (
    SystemMessageWorker,
    TaskEnvironmentBundle,
    TaskEnvironmentSession,
    TaskStatus,
    apply_task_runtime_overrides,
)

from .a2a import A2AStore
from .schemas import AutoGenConfig, AutoGenRunResult
from .workers import AutoGenCommanderWorker, AutoGenSubtaskPlanningWorker


@dataclass
class AutoGenAgentRuntime:
    agent: Any
    buffer: SingleSlotOverwriteBuffer
    observation_worker: ObservationWorker
    acting_worker: BasicActingWorker
    planning_worker: AutoGenSubtaskPlanningWorker

    @property
    def workers(self) -> tuple[Any, ...]:
        return (
            self.observation_worker,
            self.acting_worker,
            self.planning_worker,
        )


class AutoGenRunner(BasicAsyncRunner):
    """Hierarchical Commander/executor baseline with private A2A subtasks."""

    def __init__(
        self,
        *,
        session: TaskEnvironmentSession,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: AutoGenConfig | None = None,
        registry_path: str | None = None,
        runtime_closer: RuntimeCloser | None = None,
        observation_components_factory: ObservationComponentsFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        episode_recorder: EpisodeRecorder | None = None,
        a2a_store: A2AStore | None = None,
    ) -> None:
        active_config = apply_task_runtime_overrides(
            config or AutoGenConfig(), session.spec
        )
        super().__init__(
            session=session,
            llm_client=llm_client,
            memory=memory,
            config=active_config,
            registry_path=registry_path,
            runtime_closer=runtime_closer,
            observation_components_factory=observation_components_factory,
            monotonic=monotonic,
            wall_time=wall_time,
            episode_recorder=episode_recorder,
        )
        self.config: AutoGenConfig = active_config
        audit_path = (
            Path(episode_recorder.path) / "a2a_messages.jsonl"
            if episode_recorder is not None
            else None
        )
        self.a2a = a2a_store or A2AStore(
            commander_id=self.config.commander_id,
            event_sink=self._record_event,
            audit_path=audit_path,
        )
        self._commander_worker: AutoGenCommanderWorker | None = None
        self._result = AutoGenRunResult()

    @classmethod
    def from_bundle(
        cls,
        bundle: TaskEnvironmentBundle,
        *,
        llm_client: Any,
        memory: TeamMemory | None = None,
        config: AutoGenConfig | None = None,
        output: Callable[[str], None] = print,
        observation_components_factory: ObservationComponentsFactory | None = None,
        episode_recorder: EpisodeRecorder | None = None,
    ) -> "AutoGenRunner":
        active_config = config or AutoGenConfig()
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
            registry_path=str(bundle.registry_file),
            runtime_closer=bundle.close_runtime,
            observation_components_factory=observation_components_factory,
            episode_recorder=episode_recorder,
        )

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("AutoGenRunner can only start once")
            if self._closed:
                raise RuntimeError("AutoGenRunner is already closed")
            self._started = True
        try:
            self._record_event("runner.starting")
            self.session.start()
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
            self._build_agent_runtimes()
            for runtime in self._runtimes.values():
                runtime.observation_worker.start()
            self._wait_for_initial_observations()
            for runtime in self._runtimes.values():
                runtime.acting_worker.start()
            if self._commander_worker is not None:
                self._commander_worker.start()
            for runtime in self._runtimes.values():
                runtime.planning_worker.start()
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

    def wait(self) -> AutoGenRunResult:
        with self._lock:
            if not self._started:
                raise RuntimeError("AutoGenRunner has not started")
            if self._result.finished_at is not None:
                return self._result
        try:
            while not self._stop_event.is_set():
                if self._fatal_event.is_set():
                    self._set_runner_error("an AutoGen worker reached a fatal error")
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

    def run(self) -> AutoGenRunResult:
        self.start()
        return self.wait()

    def status(self) -> dict[str, Any]:
        data = super().status()
        commander = self._commander_worker
        data["commander"] = (
            None
            if commander is None
            else {
                "running": commander.is_running,
                "iterations": commander.iteration_count,
                "planned_count": commander.planned_count,
                "assignment_count": commander.assignment_count,
                "last_error": str(commander.last_error) if commander.last_error else None,
            }
        )
        data["a2a"] = {
            "message_count": self.a2a.message_count,
            "states": {
                agent_id: state.to_dict()
                for agent_id, state in self.a2a.states().items()
            },
        }
        return data

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            runtimes = list(self._runtimes.values())
        self._stop_event.set()
        cleanup_errors: list[tuple[str, str]] = []
        self._record_event("runner.stopping", strict=False, errors=cleanup_errors)
        self._stop_system_message_worker(cleanup_errors)
        workers_to_stop: list[Any] = []
        if self._commander_worker is not None:
            workers_to_stop.append(self._commander_worker)
        for runtime in runtimes:
            workers_to_stop.extend(runtime.workers)
        for worker in workers_to_stop:
            try:
                worker.stop(reason="runner_stopped", timeout=0.0)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append((worker.name, str(exc)))
        for worker in workers_to_stop:
            worker.join(timeout=self.config.worker_stop_timeout)
            if worker.is_running:
                cleanup_errors.append(
                    (worker.name, "did not stop within worker_stop_timeout")
                )
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
                runtime.planning_worker.planned_count for runtime in runtimes
            )
            self._result.executed_count = sum(
                runtime.acting_worker.executed_count for runtime in runtimes
            )
            self._result.commander_planned_count = (
                self._commander_worker.planned_count
                if self._commander_worker is not None
                else 0
            )
            self._result.subtask_report_count = sum(
                runtime.planning_worker.report_count for runtime in runtimes
            )
            self._result.a2a_message_count = self.a2a.message_count
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

    def _build_agent_runtimes(self) -> None:
        agents = list(self.session.agents)
        names = [str(agent.username) for agent in agents]
        if len(set(names)) != len(names):
            raise ValueError("AutoGen agent usernames must be unique")
        self.a2a.ensure_agents(names)
        rule_factory = self._rule_factory()
        self._commander_worker = AutoGenCommanderWorker(
            llm_client=self.llm_client,
            memory=self.memory,
            a2a=self.a2a,
            config=self.config,
            spec=self.session.spec,
            executors=names,
            budget=self._budget,
            liveness_gate=self.liveness_gate,
            event_sink=self._record_event,
        )
        self._commander_worker.on_error = self._on_worker_error
        for agent in agents:
            agent_id = str(agent.username)
            registry = agent.action_planner.registry
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
                event_sink=self._record_event,
                liveness_gate=self.liveness_gate,
            )
            buffer = SingleSlotOverwriteBuffer()
            acting = BasicActingWorker(
                agent=agent,
                buffer=buffer,
                memory=self.memory,
                config=self.config,
                instructions=self._instructions,
                broadcast=(
                    self._broadcast if self.config.minecraft_say_results else None
                ),
                event_sink=self._record_event,
                execution_table=self._execution_table,
                call_file_resolver=self._call_file_resolver,
                plan_source="autogen",
                task_category=self.session.spec.category,
                trade_action_observer=(
                    self._trade_recipe_history.record_action_result
                    if self._trade_recipe_history is not None
                    else None
                ),
                liveness_gate=self.liveness_gate,
            )
            planning = AutoGenSubtaskPlanningWorker(
                agent=agent,
                teammates=[name for name in names if name != agent_id],
                llm_client=self.llm_client,
                buffer=buffer,
                memory=self.memory,
                a2a=self.a2a,
                config=self.config,
                registry=registry,
                spec=self.session.spec,
                instructions=self._instructions,
                acting_worker=acting,
                budget=self._budget,
                event_sink=self._record_event,
                liveness_gate=self.liveness_gate,
                trade_recipe_history=self._trade_recipe_history,
            )
            for worker in (observation, acting, planning):
                worker.on_error = self._on_worker_error
            self._runtimes[agent_id] = AutoGenAgentRuntime(
                agent=agent,
                buffer=buffer,
                observation_worker=observation,
                acting_worker=acting,
                planning_worker=planning,
            )


__all__ = ["AutoGenAgentRuntime", "AutoGenRunner"]
