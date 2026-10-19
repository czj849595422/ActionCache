"""Sequential multi-episode runner for PVP matches."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from src.tasks import (
    TaskEnvironmentBundle,
    TaskEnvironmentSession,
    TaskRegistry,
    TaskSpec,
    create_default_registry,
    create_task_environment_bundle,
    resolve_task_world,
)
from src.tasks.spec import TaskSpecError

from .runner import (
    PVPMatchResult,
    PVPMatchRunner,
    PVPTeamRunConfig,
    PVPTeamRunnerResult,
    pvp_method_config,
    pvp_spec_with_team_size,
    pvp_team_episode_ids,
)


ROOT = Path(__file__).resolve().parents[3]

LLMClientFactory = Callable[[Any], Any]
BundleFactory = Callable[[TaskSpec, Any, Any], TaskEnvironmentBundle]
SessionFactory = Callable[..., Any]
MatchRunnerFactory = Callable[..., Any]


@dataclass
class PVPSequentialEpisodeResult:
    task_id: str
    episode_id: str
    result: Any

    def to_dict(self) -> dict[str, Any]:
        to_dict = getattr(self.result, "to_dict", None)
        return {
            "task_id": self.task_id,
            "episode_id": self.episode_id,
            "result": to_dict() if callable(to_dict) else _json_value(self.result),
        }


@dataclass
class PVPSequentialResult:
    episodes: list[PVPSequentialEpisodeResult] = field(default_factory=list)
    interrupted: bool = False

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        if not self.episodes:
            return 1
        return 0 if all(_result_exit_code(item.result) == 0 for item in self.episodes) else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_count": len(self.episodes),
            "interrupted": self.interrupted,
            "exit_code": self.exit_code,
            "episodes": [item.to_dict() for item in self.episodes],
        }


class PVPSequentialRunner:
    """Run multiple PVP matches in one recording run.

    Each episode gets a fresh environment, manager, team recorders, LLM clients,
    and PVPMatchRunner. The caller is responsible for finalizing run_recorder.
    """

    def __init__(
        self,
        *,
        specs: Sequence[TaskSpec],
        team_a_method: str,
        team_b_method: str,
        llm_client_factory: LLMClientFactory,
        run_recorder: Any,
        server_dir: str | Path,
        bridge_port_start: int,
        benchmark_root: str | Path,
        registry: TaskRegistry | None = None,
        team_size: int | None = None,
        max_run_seconds: float | None = None,
        max_llm_calls: int | None = None,
        max_llm_calls_per_agent: int | None = None,
        task_poll_interval: float = 0.2,
        prepare_seconds: float = 10.0,
        unsafe_actions: bool = False,
        enable_chat_actions: bool = False,
        broadcast_results: bool = True,
        system_messages_enabled: bool = False,
        system_message_interval: float = 30.0,
        llm_config_metadata: Mapping[str, Any] | None = None,
        output: Callable[[str], None] = print,
        start_episode: int = 1,
        episode_count: int | None = None,
        bundle_factory: BundleFactory | None = None,
        session_factory: SessionFactory | None = None,
        match_runner_factory: MatchRunnerFactory | None = None,
    ) -> None:
        if not specs:
            raise ValueError("specs must not be empty")
        for spec in specs:
            if spec.category != "pvp":
                raise TaskSpecError("PVPSequentialRunner only supports category=pvp")
        if team_size is not None and team_size < 1:
            raise ValueError("team_size must be >= 1")
        if max_run_seconds is not None and max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive when provided")
        if task_poll_interval <= 0:
            raise ValueError("task_poll_interval must be positive")
        if prepare_seconds < 0:
            raise ValueError("prepare_seconds must be non-negative")
        if system_message_interval <= 0:
            raise ValueError("system_message_interval must be positive")
        self.specs = list(specs)
        self.team_a_method = team_a_method
        self.team_b_method = team_b_method
        self.llm_client_factory = llm_client_factory
        self.run_recorder = run_recorder
        self.server_dir = Path(server_dir)
        self.bridge_port_start = bridge_port_start
        self.benchmark_root = Path(benchmark_root)
        self.registry = registry or create_default_registry()
        self.team_size = team_size
        self.max_run_seconds = max_run_seconds
        self.max_llm_calls = max_llm_calls
        self.max_llm_calls_per_agent = max_llm_calls_per_agent
        self.task_poll_interval = task_poll_interval
        self.prepare_seconds = prepare_seconds
        self.unsafe_actions = unsafe_actions
        self.enable_chat_actions = enable_chat_actions
        self.broadcast_results = broadcast_results
        self.system_messages_enabled = system_messages_enabled
        self.system_message_interval = system_message_interval
        self.llm_config_metadata = dict(llm_config_metadata or {})
        self.output = output
        self._bundle_factory = bundle_factory or self._default_bundle
        self._session_factory = session_factory or TaskEnvironmentSession
        self._match_runner_factory = match_runner_factory or PVPMatchRunner
        self.start_episode = 1
        self.episode_count: int | None = None
        self.configure_window(
            start_episode=start_episode,
            episode_count=episode_count,
        )

    def configure_window(
        self,
        *,
        start_episode: int = 1,
        episode_count: int | None = None,
    ) -> None:
        if start_episode < 1:
            raise ValueError("start_episode must be >= 1")
        if episode_count is not None and episode_count < 1:
            raise ValueError("episode_count must be >= 1")
        if episode_count is not None and episode_count > len(self.specs):
            raise ValueError(
                f"episode_count={episode_count} exceeds specs count {len(self.specs)}"
            )
        self.start_episode = start_episode
        self.episode_count = episode_count

    def run(
        self,
        *,
        start_episode: int | None = None,
        episode_count: int | None = None,
    ) -> PVPSequentialResult:
        if start_episode is not None or episode_count is not None:
            self.configure_window(
                start_episode=start_episode or self.start_episode,
                episode_count=(
                    self.episode_count if episode_count is None else episode_count
                ),
            )
        self._check_resume_count()
        count = self.episode_count or len(self.specs)
        total = self.start_episode + count - 1
        result = PVPSequentialResult()
        for index in range(count):
            spec = self._episode_spec(self.specs[index])
            episode_number = self.start_episode + index
            episode_result = self._run_episode(
                spec,
                episode_number=episode_number,
                total=total,
            )
            result.episodes.append(episode_result)
            if getattr(episode_result.result, "interrupted", False):
                result.interrupted = True
                break
        return result

    def _default_bundle(
        self,
        spec: TaskSpec,
        manager: Any,
        episode: Any,
    ) -> TaskEnvironmentBundle:
        source_world = resolve_task_world(spec, self.benchmark_root)
        return create_task_environment_bundle(
            spec=spec,
            manager=manager,
            source_world=source_world,
            server_dir=self.server_dir,
            bridge_port_start=self.bridge_port_start,
            log_dir=episode.logs_dir,
            runtime_root=ROOT / ".tmp",
            unsafe_actions=self.unsafe_actions,
        )

    def _check_resume_count(self) -> None:
        existing = getattr(self.run_recorder, "episode_count", None)
        if existing is not None and existing != self.start_episode - 1:
            raise ValueError(
                f"run 已有 {existing} 个 episode，start_episode={self.start_episode} "
                f"要求已有 {self.start_episode - 1} 个"
            )

    def _episode_spec(self, spec: TaskSpec) -> TaskSpec:
        if self.team_size is None:
            return spec
        current = int(spec.agents.get("count", 0)) // 2
        if current == self.team_size:
            return spec
        return pvp_spec_with_team_size(spec, self.team_size)

    def _run_episode(
        self,
        spec: TaskSpec,
        *,
        episode_number: int,
        total: int,
    ) -> PVPSequentialEpisodeResult:
        self.output(
            f"episode {episode_number}/{total}: {spec.task_id} "
            f"({self.team_a_method} vs {self.team_b_method})"
        )
        episode = self.run_recorder.start_episode(
            episode_id=str(episode_number),
            metadata=self._episode_metadata(spec, episode_number),
        )
        team_a_recorder = None
        team_b_recorder = None
        client_a = None
        client_b = None
        bundle = None
        match = None
        failure: str | None = None
        run_result: Any | None = None
        interrupted = False
        try:
            team_a_episode_id, team_b_episode_id = pvp_team_episode_ids(
                self.team_a_method,
                self.team_b_method,
            )
            team_a_recorder = episode.team_episode(
                team_a_episode_id,
                baseline=self.team_a_method,
                metadata={
                    "team_id": "alpha",
                    "method": self.team_a_method,
                    "config_index": episode_number,
                },
            )
            team_b_recorder = episode.team_episode(
                team_b_episode_id,
                baseline=self.team_b_method,
                metadata={
                    "team_id": "bravo",
                    "method": self.team_b_method,
                    "config_index": episode_number,
                },
            )
            client_a = self.llm_client_factory(team_a_recorder.llm_calls)
            client_b = self.llm_client_factory(team_b_recorder.llm_calls)
            config_a = self._method_config(self.team_a_method)
            config_b = self._method_config(self.team_b_method)
            manager = self.registry.manager_for(spec.category)()
            bundle = self._bundle_factory(spec, manager, episode)
            session = self._session_factory(
                spec=bundle.spec,
                manager=bundle.manager,
                server=bundle.server,
                agents=bundle.agents,
                world_workspace=bundle.world_workspace,
                prepare_seconds=self.prepare_seconds,
                output=self.output,
            )
            match = self._match_runner_factory(
                session=session,
                teams=(
                    PVPTeamRunConfig(
                        team_id="alpha",
                        method=self.team_a_method,
                        llm_client=client_a,
                        config=config_a,
                        episode_recorder=team_a_recorder,
                    ),
                    PVPTeamRunConfig(
                        team_id="bravo",
                        method=self.team_b_method,
                        llm_client=client_b,
                        config=config_b,
                        episode_recorder=team_b_recorder,
                    ),
                ),
                registry_path=str(bundle.registry_file),
                episode_recorder=episode,
                max_run_seconds=self.max_run_seconds,
                task_poll_interval=self.task_poll_interval,
                output=self.output,
            )
            run_result = match.run()
            interrupted = bool(getattr(run_result, "interrupted", False))
        except KeyboardInterrupt:
            failure = "KeyboardInterrupt"
            interrupted = True
            run_result = _failure_result(
                failure,
                self.team_a_method,
                self.team_b_method,
                interrupted=True,
            )
            self.output("PVP sequential run interrupted")
        except Exception as exc:  # noqa: BLE001
            failure = str(exc)
            run_result = _failure_result(
                failure,
                self.team_a_method,
                self.team_b_method,
            )
            self.output(f"episode {episode_number} failed: {failure}")
        finally:
            if bundle is not None:
                try:
                    bundle.close_runtime()
                except Exception as exc:  # noqa: BLE001
                    failure = failure or str(exc)
            for client in (client_a, client_b):
                if client is None:
                    continue
                close = getattr(client, "close", None)
                if not callable(close):
                    continue
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    failure = failure or str(exc)
            if run_result is None:
                run_result = _failure_result(
                    failure or "PVP runner did not produce a terminal result",
                    self.team_a_method,
                    self.team_b_method,
                    interrupted=interrupted,
                )
            self._finalize_started_recorders(
                episode,
                team_a_recorder,
                team_b_recorder,
                run_result=run_result,
                failure=failure,
                interrupted=interrupted,
            )
        return PVPSequentialEpisodeResult(
            task_id=spec.task_id,
            episode_id=episode.context.episode_id,
            result=run_result,
        )

    def _method_config(self, method: str) -> Any:
        return pvp_method_config(
            method,
            max_run_seconds=self.max_run_seconds,
            max_llm_calls=self.max_llm_calls,
            max_llm_calls_per_agent=self.max_llm_calls_per_agent,
            enable_chat_actions=self.enable_chat_actions,
            broadcast_results=self.broadcast_results,
            system_messages_enabled=self.system_messages_enabled,
            system_message_interval=self.system_message_interval,
            task_poll_interval=self.task_poll_interval,
        )

    def _episode_metadata(
        self,
        spec: TaskSpec,
        episode_number: int,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "task_spec": spec.to_dict(),
            "agents": list(spec.agents.get("names") or []),
            "team_a_method": self.team_a_method,
            "team_b_method": self.team_b_method,
            "team_size": int(spec.agents.get("count", 0)) // 2,
            "config_index": episode_number,
            "team_a_config": _config_payload(self._method_config(self.team_a_method)),
            "team_b_config": _config_payload(self._method_config(self.team_b_method)),
        }
        if self.llm_config_metadata:
            metadata["llm_config"] = dict(self.llm_config_metadata)
        return metadata

    def _finalize_started_recorders(
        self,
        episode: Any,
        team_a_recorder: Any | None,
        team_b_recorder: Any | None,
        *,
        run_result: Any,
        failure: str | None,
        interrupted: bool,
    ) -> None:
        status = (
            "interrupted"
            if interrupted
            else "completed"
            if getattr(run_result, "exit_code", 1) == 0 and not failure
            else "failed"
        )
        for recorder in (team_a_recorder, team_b_recorder):
            if recorder is None or getattr(recorder, "status", None) != "started":
                continue
            recorder.finalize(status=status, error=failure)
        if getattr(episode, "status", None) == "started":
            episode.finalize(
                status=status,
                result=run_result,
                error=failure,
                metadata={
                    "team_results": _team_results_payload(run_result),
                },
            )


def _failure_result(
    message: str,
    team_a_method: str,
    team_b_method: str,
    *,
    interrupted: bool = False,
) -> PVPMatchResult:
    now = time.time()
    return PVPMatchResult(
        interrupted=interrupted,
        runner_error=None if interrupted else message,
        stop_reason="keyboard_interrupt" if interrupted else "runner_error",
        cleanup_errors=[],
        team_results=[
            PVPTeamRunnerResult(
                team_id="alpha",
                method=team_a_method,
                error=None if interrupted else message,
            ),
            PVPTeamRunnerResult(
                team_id="bravo",
                method=team_b_method,
                error=None if interrupted else message,
            ),
        ],
        started_at=now,
        finished_at=now,
    )


def _result_exit_code(result: Any) -> int:
    value = getattr(result, "exit_code", None)
    if isinstance(value, int):
        return value
    if isinstance(result, Mapping):
        value = result.get("exit_code")
        if isinstance(value, int):
            return value
    return 1


def _team_results_payload(result: Any) -> list[dict[str, Any]]:
    values = getattr(result, "team_results", None)
    if not isinstance(values, list):
        return []
    payloads: list[dict[str, Any]] = []
    for item in values:
        to_dict = getattr(item, "to_dict", None)
        payloads.append(to_dict() if callable(to_dict) else _json_value(item))
    return payloads


def _config_payload(config: Any) -> Any:
    if is_dataclass(config):
        return asdict(config)
    to_dict = getattr(config, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(config, Mapping):
        return dict(config)
    return _json_value(config)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)


__all__ = [
    "PVPSequentialEpisodeResult",
    "PVPSequentialResult",
    "PVPSequentialRunner",
]
