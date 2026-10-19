"""Two-team PVP match orchestration built around existing method runners."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, is_dataclass, replace
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from src.baseline.AgenticCache import AgenticCacheConfig, AgenticCacheRunner
from src.baseline.AutoGen import AutoGenConfig, AutoGenRunner
from src.baseline.BasicAsync import BasicAsyncConfig, BasicAsyncRunner
from src.baseline.COAP import COAPConfig, COAPRunner
from src.baseline.DirectAction import DirectActionConfig, DirectActionRunner
from src.baseline.Parallelized import PPAConfig, PPARunner
from src.memory import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory
from src.recording import EpisodeRecorder
from src.tasks.environment_runner import (
    TaskEnvironmentPoll,
    TaskEnvironmentSession,
)
from src.tasks.manager import TaskOutcome, TaskStatus
from src.tasks.pvp.spawns import pvp_task_data_with_seeded_spawns
from src.tasks.spec import TaskSpec, TaskSpecError

RunnerFactory = Callable[..., Any]
_PLANNING_START_GATE_METHODS = frozenset({"basic_async", "direct_action"})

_ALPHA_AGENT_NAMES = (
    "Aria",
    "Blake",
    "Cora",
    "Dax",
    "Iris",
    "Juno",
    "Kade",
    "Lena",
    "Milo",
    "Nora",
    "Oren",
    "Pia",
    "Quinn",
    "Rhea",
    "Seth",
    "Tara",
)
_BRAVO_AGENT_NAMES = (
    "Eli",
    "Faye",
    "Grey",
    "Hugo",
    "Isla",
    "Jace",
    "Kira",
    "Luca",
    "Maya",
    "Noel",
    "Opal",
    "Rory",
    "Sage",
    "Tess",
    "Uma",
    "Vera",
)


@dataclass(frozen=True)
class PVPTeamRunConfig:
    team_id: str
    method: str
    llm_client: Any
    config: Any | None = None
    memory: TeamMemory | None = None
    episode_recorder: EpisodeRecorder | None = None


@dataclass
class PVPTeamRunnerResult:
    team_id: str
    method: str
    result: Any | None = None
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.error:
            return 1
        value = self.result
        code = getattr(value, "exit_code", None)
        return int(code) if isinstance(code, int) else 0

    def to_dict(self) -> dict[str, Any]:
        result_value = self.result
        to_dict = getattr(result_value, "to_dict", None)
        return {
            "team_id": self.team_id,
            "method": self.method,
            "result": to_dict() if callable(to_dict) else _json_value(result_value),
            "error": self.error,
            "exit_code": self.exit_code,
        }


@dataclass
class PVPMatchResult:
    outcome: TaskOutcome | None = None
    final_metrics: dict[str, Any] | None = None
    interrupted: bool = False
    stop_reason: str | None = None
    runner_error: str | None = None
    cleanup_errors: list[tuple[str, str]] | None = None
    team_results: list[PVPTeamRunnerResult] | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        if self.runner_error or self.cleanup_errors:
            return 1
        if any(result.exit_code for result in self.team_results or []):
            return 1
        if self.outcome is None or self.outcome.status != TaskStatus.SUCCESS:
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "final_metrics": dict(self.final_metrics or {}),
            "interrupted": self.interrupted,
            "stop_reason": self.stop_reason,
            "runner_error": self.runner_error,
            "cleanup_errors": [list(item) for item in self.cleanup_errors or []],
            "team_results": [
                item.to_dict() for item in self.team_results or []
            ],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }


class _PVPPlanningStartGate:
    """Synchronize the first planning request across both PVP teams."""

    def __init__(
        self,
        team_ids: Sequence[str],
        on_release: Callable[[], None] | None = None,
    ) -> None:
        unique_ids = tuple(dict.fromkeys(str(team_id) for team_id in team_ids))
        if len(unique_ids) < 2:
            raise ValueError("planning start gate requires at least two teams")
        self._pending = set(unique_ids)
        self._released = False
        self._aborted: str | None = None
        self._on_release = on_release
        self._condition = threading.Condition()

    def wait_for_team(self, team_id: str) -> Callable[[str], None]:
        team_key = str(team_id)

        def wait(_agent_id: str) -> None:
            self.wait(team_key)

        return wait

    def wait(self, team_id: str) -> None:
        on_release: Callable[[], None] | None = None
        with self._condition:
            if self._released:
                return
            if self._aborted is not None:
                raise RuntimeError(self._aborted)
            self._pending.discard(str(team_id))
            if not self._pending:
                self._released = True
                on_release = self._on_release
                self._condition.notify_all()
            else:
                while not self._released and self._aborted is None:
                    self._condition.wait()
                if self._aborted is not None:
                    raise RuntimeError(self._aborted)
        if on_release is not None:
            on_release()

    def abort(self, reason: str) -> None:
        with self._condition:
            if self._released:
                return
            self._aborted = reason
            self._condition.notify_all()


class _PVPPlanningGateLLMClient:
    """Delay a team's first LLM request until both PVP teams are ready."""

    def __init__(
        self,
        delegate: Any,
        *,
        gate: _PVPPlanningStartGate,
        team_id: str,
    ) -> None:
        self._delegate = delegate
        self._gate = gate
        self._team_id = str(team_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self._gate.wait(self._team_id)
        return self._delegate.complete(*args, **kwargs)

    async def acomplete(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.to_thread(self._gate.wait, self._team_id)
        return await self._delegate.acomplete(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        self._gate.wait(self._team_id)
        return self._delegate.stream(*args, **kwargs)

    async def astart_stream(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.to_thread(self._gate.wait, self._team_id)
        return await self._delegate.astart_stream(*args, **kwargs)


class _PVPTeamMemoryFanout:
    """Forward task-level system messages into each team's private memory."""

    def __init__(self, memories: Sequence[Any]) -> None:
        unique: list[Any] = []
        seen: set[int] = set()
        for memory in memories:
            if memory is None:
                continue
            key = id(memory)
            if key in seen:
                continue
            seen.add(key)
            unique.append(memory)
        self._memories = tuple(unique)

    def append_history_event(self, **kwargs: Any) -> Any:
        last_entry = None
        for memory in self._memories:
            last_entry = memory.append_history_event(**kwargs)
        return last_entry


class PVPTeamSessionProxy:
    """Expose one team's agents while delegating environment state to a shared session."""

    def __init__(
        self,
        shared: TaskEnvironmentSession,
        *,
        agents: Sequence[Any],
        memory: TeamMemory,
    ) -> None:
        self._shared = shared
        self.spec = shared.spec
        self.manager = shared.manager
        self.server = shared.server
        self.agents = list(agents)
        self.world_workspace = shared.world_workspace
        self.keep_server = shared.keep_server
        self.prepare_seconds = shared.prepare_seconds
        self.output = shared.output
        self.memory = memory
        self._started = False

    @property
    def context(self) -> Any:
        return self._shared.context

    @property
    def outcome(self) -> Any:
        return self._shared.outcome

    def start(self) -> Any:
        if self._started:
            raise RuntimeError("PVPTeamSessionProxy can only start once")
        self._started = True
        context = self._shared.context
        if context is None:
            raise RuntimeError("shared PVP environment session has not started")
        return context

    def poll(self) -> TaskEnvironmentPoll:
        return self._shared.poll()

    def snapshot(self) -> dict[str, Any]:
        return self._shared.snapshot()

    def close(self) -> list[tuple[str, str]]:
        return []


class PVPMatchRunner:
    """Run two existing method runners against one shared PVP environment."""

    def __init__(
        self,
        *,
        session: TaskEnvironmentSession,
        teams: tuple[PVPTeamRunConfig, PVPTeamRunConfig],
        registry_path: str | None = None,
        episode_recorder: EpisodeRecorder | None = None,
        max_run_seconds: float | None = None,
        task_poll_interval: float = 0.2,
        output: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if len(teams) != 2:
            raise ValueError("PVPMatchRunner requires exactly two teams")
        if max_run_seconds is not None and max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive when provided")
        if task_poll_interval <= 0:
            raise ValueError("task_poll_interval must be positive")
        self.session = session
        self.teams = teams
        self.registry_path = registry_path
        self.episode_recorder = episode_recorder
        self.max_run_seconds = max_run_seconds
        self.task_poll_interval = task_poll_interval
        self.output = output
        self._sleep = sleep
        self._wall_time = wall_time
        self._started = False
        self._closed = False
        self._team_runners: list[Any] = []
        self._team_threads: list[threading.Thread] = []
        self._team_results: list[PVPTeamRunnerResult] = []
        self._result = PVPMatchResult(cleanup_errors=[], team_results=[])
        self._lock = threading.RLock()
        self._planning_start_gate = self._create_planning_start_gate()

    def run(self) -> PVPMatchResult:
        if self._started:
            raise RuntimeError("PVPMatchRunner can only run once")
        self._started = True
        try:
            self._record_event("pvp_match.starting")
            self._ensure_team_memories()
            self.session.start()
            self._build_team_runners()
            self._start_team_runners()
            self._start_team_wait_threads()
            self._result.outcome = self._wait_for_terminal()
            self._result.stop_reason = self._result.stop_reason or "task_outcome"
            self._record_event(
                "pvp_match.terminal",
                outcome=self._result.outcome.to_dict() if self._result.outcome else None,
                metrics=dict(self._result.final_metrics or {}),
            )
        except KeyboardInterrupt:
            self._result.interrupted = True
            self._result.stop_reason = "keyboard_interrupt"
            self._record_event("pvp_match.interrupted", strict=False)
        except Exception as exc:  # noqa: BLE001
            self._result.runner_error = str(exc)
            self._result.stop_reason = self._result.stop_reason or "runner_error"
            self._record_event("pvp_match.failed", strict=False, error=str(exc))
        finally:
            self.close()
            self._result.finished_at = self._wall_time()
            self._result.team_results = list(self._team_results)
            if self.episode_recorder is not None:
                self._finalize_episode()
        return self._result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runners = list(self._team_runners)
            threads = list(self._team_threads)
        errors: list[tuple[str, str]] = []
        if self._planning_start_gate is not None:
            self._planning_start_gate.abort("PVP match is closing before planning gate release")
        for runner in runners:
            close = getattr(runner, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                errors.append((runner.__class__.__name__, str(exc)))
        for thread in threads:
            thread.join(timeout=8.0)
            if thread.is_alive():
                errors.append((thread.name, "did not stop within timeout"))
        try:
            errors.extend(self.session.close())
        except Exception as exc:  # noqa: BLE001
            errors.append(("shared_session", str(exc)))
        self._result.cleanup_errors = list(self._result.cleanup_errors or []) + errors

    def _build_team_runners(self) -> None:
        self._ensure_team_memories()
        roster = _team_agents_by_id(self.session.spec, self.session.agents)
        for team in self.teams:
            agents = roster.get(team.team_id)
            if not agents:
                raise TaskSpecError(f"PVP team not found or empty: {team.team_id}")
            memory = team.memory
            if memory is None:
                raise RuntimeError("PVP team memory was not initialized")
            proxy = PVPTeamSessionProxy(
                self.session,
                agents=agents,
                memory=memory,
            )
            runner = build_pvp_method_runner(
                team.method,
                session=proxy,
                llm_client=team.llm_client,
                memory=memory,
                config=team.config,
                registry_path=self.registry_path,
                episode_recorder=team.episode_recorder,
                planning_start_gate=self._planning_start_gate,
                team_id=team.team_id,
            )
            self._team_runners.append(runner)

    def _ensure_team_memories(self) -> None:
        memories: list[TeamMemory] = []
        teams: list[PVPTeamRunConfig] = []
        for team in self.teams:
            memory = team.memory or TeamMemory(
                default_chat_window=DEFAULT_TEAM_CHAT_WINDOW
            )
            teams.append(replace(team, memory=memory))
            memories.append(memory)
        self.teams = (teams[0], teams[1])
        self.session.memory = _PVPTeamMemoryFanout(memories)

    def _create_planning_start_gate(self) -> _PVPPlanningStartGate | None:
        return _PVPPlanningStartGate(
            [team.team_id for team in self.teams],
            on_release=self._mark_combat_started,
        )

    def _mark_combat_started(self) -> None:
        with self._lock:
            if self._result.started_at is not None:
                return
            self._result.started_at = self._wall_time()
        self._record_event("pvp_match.started")

    def _start_team_runners(self) -> None:
        runners = list(self._team_runners)
        if not runners:
            return
        start_barrier = threading.Barrier(len(runners))
        errors: list[tuple[str, str]] = []
        lock = threading.Lock()

        def start_team(team: PVPTeamRunConfig, runner: Any) -> None:
            try:
                start_barrier.wait()
                runner.start()
            except Exception as exc:  # noqa: BLE001
                if self._planning_start_gate is not None:
                    self._planning_start_gate.abort(
                        f"PVP team runner startup failed: {team.team_id}: {exc}"
                    )
                with lock:
                    errors.append((team.team_id, str(exc)))

        threads = [
            threading.Thread(
                target=start_team,
                args=(team, runner),
                name=f"pvp-start-{team.team_id}-{_normalize_method(team.method)}",
                daemon=True,
            )
            for team, runner in zip(self.teams, runners)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            details = "; ".join(f"{team}: {error}" for team, error in errors)
            raise RuntimeError(f"PVP team runner startup failed: {details}")

    def _start_team_wait_threads(self) -> None:
        for team, runner in zip(self.teams, self._team_runners):
            thread = threading.Thread(
                target=self._wait_team_runner,
                args=(team, runner),
                name=f"pvp-{team.team_id}-{_normalize_method(team.method)}",
                daemon=True,
            )
            self._team_threads.append(thread)
            thread.start()

    def _wait_team_runner(self, team: PVPTeamRunConfig, runner: Any) -> None:
        result = PVPTeamRunnerResult(team_id=team.team_id, method=team.method)
        try:
            result.result = runner.wait()
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
        with self._lock:
            self._team_results.append(result)

    def _wait_for_terminal(self) -> TaskOutcome | None:
        while True:
            poll = self.session.poll()
            self._result.final_metrics = dict(poll.metrics)
            if poll.outcome.status != TaskStatus.RUNNING:
                return poll.outcome
            started_at = self._result.started_at
            if self.max_run_seconds is not None and started_at is not None:
                elapsed = self._wall_time() - started_at
                if elapsed >= self.max_run_seconds:
                    self._result.stop_reason = "max_run_seconds"
                    return poll.outcome
            self._sleep(self.task_poll_interval)

    def _finalize_episode(self) -> None:
        if self.episode_recorder is None or self.episode_recorder.status != "started":
            return
        status = (
            "interrupted"
            if self._result.interrupted
            else "completed"
            if self._result.exit_code == 0
            else "failed"
        )
        self.episode_recorder.finalize(
            status=status,
            result=self._result,
            error=self._result.runner_error,
            metadata={
                "team_results": [
                    item.to_dict() for item in self._team_results
                ],
            },
        )

    def _record_event(self, event_type: str, **data: Any) -> None:
        if self.episode_recorder is None:
            return
        strict = bool(data.pop("strict", True))
        try:
            self.episode_recorder.event(event_type, **data)
        except Exception:
            if strict:
                raise


def pvp_method_names() -> tuple[str, ...]:
    return tuple(sorted(_RUNNER_FACTORIES))


def normalize_pvp_method_name(method: str) -> str:
    value = str(method or "").strip().lower().replace("-", "_")
    aliases = {
        "basic": "basic_async",
        "async": "basic_async",
        "auto_gen": "autogen",
        "ppa": "parallelized",
        "parallel": "parallelized",
        "parallelized_planning_acting": "parallelized",
        "direct": "direct_action",
        "agentic": "agentic_cache",
    }
    return aliases.get(value, value)


def pvp_team_episode_ids(method_a: str, method_b: str) -> tuple[str, str]:
    normalized_a = normalize_pvp_method_name(method_a)
    normalized_b = normalize_pvp_method_name(method_b)
    if normalized_a == normalized_b:
        return f"team-{normalized_a}-1", f"team-{normalized_b}-2"
    return f"team-{normalized_a}", f"team-{normalized_b}"


def pvp_matchup_id(method_a: str, method_b: str) -> str:
    normalized_a = normalize_pvp_method_name(method_a)
    normalized_b = normalize_pvp_method_name(method_b)
    return f"{normalized_a}_vs_{normalized_b}"


def pvp_method_config(
    method: str,
    *,
    max_run_seconds: float | None = None,
    max_llm_calls: int | None = None,
    max_llm_calls_per_agent: int | None = None,
    enable_chat_actions: bool = False,
    broadcast_results: bool = True,
    system_messages_enabled: bool = False,
    system_message_interval: float = 30.0,
    task_poll_interval: float = 0.2,
) -> Any:
    normalized = _normalize_method(method)
    common = {
        "max_run_seconds": max_run_seconds,
        "max_llm_calls": max_llm_calls,
        "max_llm_calls_per_agent": max_llm_calls_per_agent,
        "broadcast_results": broadcast_results,
        "task_poll_interval": task_poll_interval,
    }
    if normalized == "basic_async":
        return BasicAsyncConfig(
            **common,
            enable_chat_actions=enable_chat_actions,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
        )
    if normalized == "direct_action":
        return DirectActionConfig(
            **common,
            enable_chat_actions=enable_chat_actions,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
        )
    if normalized == "parallelized":
        return PPAConfig(
            **common,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
        )
    if normalized == "coap":
        return COAPConfig(
            **common,
            enable_chat_actions=enable_chat_actions,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
        )
    if normalized == "agentic_cache":
        return AgenticCacheConfig(
            **common,
            enable_chat_actions=enable_chat_actions,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
        )
    if normalized == "autogen":
        return AutoGenConfig(
            **common,
            enable_chat_actions=enable_chat_actions,
            system_messages_enabled=system_messages_enabled,
            system_message_interval=system_message_interval,
            minecraft_say_results=broadcast_results,
        )
    raise ValueError(f"unknown PVP method: {method}")


def build_pvp_method_runner(
    method: str,
    *,
    session: PVPTeamSessionProxy,
    llm_client: Any,
    memory: TeamMemory,
    config: Any | None,
    registry_path: str | None,
    episode_recorder: EpisodeRecorder | None,
    planning_start_gate: _PVPPlanningStartGate | None = None,
    team_id: str | None = None,
) -> Any:
    normalized = _normalize_method(method)
    factory = _RUNNER_FACTORIES.get(normalized)
    if factory is None:
        raise ValueError(f"unknown PVP method: {method}")
    active_config = config or pvp_method_config(normalized)
    active_llm_client = llm_client
    if planning_start_gate is not None:
        if not team_id:
            raise ValueError("team_id is required when using a planning start gate")
        active_llm_client = _PVPPlanningGateLLMClient(
            llm_client,
            gate=planning_start_gate,
            team_id=team_id,
        )
    if episode_recorder is not None:
        bind_recorder = getattr(active_llm_client, "bind_call_recorder", None)
        if callable(bind_recorder):
            bind_recorder(episode_recorder.llm_calls)
    runner_kwargs: dict[str, Any] = {}
    if planning_start_gate is not None and normalized in _PLANNING_START_GATE_METHODS:
        runner_kwargs["planning_worker_kwargs"] = {
            "planning_start_gate": planning_start_gate.wait_for_team(team_id)
        }
    return factory(
        session=session,
        llm_client=active_llm_client,
        memory=memory,
        config=active_config,
        registry_path=registry_path,
        episode_recorder=episode_recorder,
        **runner_kwargs,
    )


def pvp_spec_with_team_size(spec: TaskSpec, team_size: int) -> TaskSpec:
    if team_size < 1:
        raise ValueError("team_size must be >= 1")
    if team_size > min(len(_ALPHA_AGENT_NAMES), len(_BRAVO_AGENT_NAMES)):
        raise ValueError("team_size exceeds built-in unique PVP agent names")
    data = spec.to_dict()
    alpha = list(_ALPHA_AGENT_NAMES[:team_size])
    bravo = list(_BRAVO_AGENT_NAMES[:team_size])
    data["agents"] = {
        **dict(data.get("agents") or {}),
        "count": team_size * 2,
        "names": [*alpha, *bravo],
        "teams": [
            {"id": "alpha", "agents": alpha},
            {"id": "bravo", "agents": bravo},
        ],
    }
    metadata = dict(data.get("metadata") or {})
    metadata["team_size"] = team_size
    data["metadata"] = metadata
    data = pvp_task_data_with_seeded_spawns(data)
    result = TaskSpec.from_dict(data)
    result._source_path = spec.source_path
    return result


def _team_agents_by_id(
    spec: TaskSpec,
    agents: Sequence[Any],
) -> dict[str, list[Any]]:
    by_name = {str(getattr(agent, "username", agent)): agent for agent in agents}
    result: dict[str, list[Any]] = {}
    for team in (spec.agents.get("teams") or []):
        if not isinstance(team, Mapping):
            continue
        team_id = str(team.get("id") or team.get("name") or "")
        members = [str(name) for name in team.get("agents") or team.get("members") or []]
        result[team_id] = [
            by_name[name]
            for name in members
            if name in by_name
        ]
    return result


def _normalize_method(method: str) -> str:
    return normalize_pvp_method_name(method)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return str(value)


_RUNNER_FACTORIES: dict[str, RunnerFactory] = {
    "agentic_cache": AgenticCacheRunner,
    "autogen": AutoGenRunner,
    "basic_async": BasicAsyncRunner,
    "coap": COAPRunner,
    "direct_action": DirectActionRunner,
    "parallelized": PPARunner,
}


__all__ = [
    "PVPMatchResult",
    "PVPMatchRunner",
    "PVPTeamRunConfig",
    "PVPTeamRunnerResult",
    "PVPTeamSessionProxy",
    "build_pvp_method_runner",
    "normalize_pvp_method_name",
    "pvp_method_config",
    "pvp_method_names",
    "pvp_matchup_id",
    "pvp_spec_with_team_size",
    "pvp_team_episode_ids",
]
