"""多 baseline 共用的顺序多 episode 运行器（泛型）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from src.tasks import (
    TaskEnvironmentBundle,
    TaskRegistry,
    TaskSpec,
    create_default_registry,
    create_task_environment_bundle,
    resolve_task_world,
)


ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SequentialBaselineEpisodeResult:
    task_id: str
    episode_id: str
    result: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "episode_id": self.episode_id,
            "result": self.result.to_dict(),
        }


@dataclass
class SequentialBaselineResult:
    episodes: list[SequentialBaselineEpisodeResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if not self.episodes:
            return 1
        return 0 if all(item.result.exit_code == 0 for item in self.episodes) else 1

    @property
    def process_exit_code(self) -> int:
        """Return a process code that distinguishes task outcomes from crashes.

        A timeout or task-level failure is a valid experiment terminal state and
        must not prevent the next method from running. Runner, worker,
        missing-outcome, and interrupt states remain process-level failures.
        Cleanup diagnostics are warnings attached to the episode result and do
        not invalidate its task outcome.
        """
        if not self.episodes:
            return 1
        for episode in self.episodes:
            result = episode.result
            if getattr(result, "interrupted", False):
                return 130
            if any(
                getattr(result, name, None)
                for name in ("runner_error", "worker_errors")
            ):
                return 1
            if getattr(result, "outcome", None) is None:
                return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_count": len(self.episodes),
            "exit_code": self.exit_code,
            "process_exit_code": self.process_exit_code,
            "episodes": [item.to_dict() for item in self.episodes],
        }


BundleFactory = Callable[[TaskSpec, Any, Any], TaskEnvironmentBundle]
RunnerKwargsFactory = Callable[[TaskSpec, Any, Any, int], dict[str, Any]]
LLMClientFactory = Callable[[], Any]


class SequentialBaselineRunner:
    """按序读取同一任务的多个初始化文件，每个文件运行一个 episode。

    episode 记录共享同一个 ``run_recorder``（episode 目录按配置序号 1/2/3 编号，
    保存在对应任务文件夹内）；可通过 ``llm_client_factory`` 为每个 episode
    创建独立 LLM client 并绑定调用记录器。
    ``runner_class`` 为任意带 ``from_bundle(bundle, llm_client, config, episode_recorder)``
    的 baseline runner（PPA / COAP / BasicAsync）。
    """

    def __init__(
        self,
        *,
        runner_class: type[Any],
        specs: Sequence[TaskSpec],
        llm_client: Any = None,
        llm_client_factory: LLMClientFactory | None = None,
        config: Any,
        run_recorder: Any,
        server_dir: str | Path,
        bridge_port_start: int,
        benchmark_root: str | Path,
        registry: TaskRegistry | None = None,
        bundle_factory: BundleFactory | None = None,
        runner_kwargs_factory: RunnerKwargsFactory | None = None,
        output: Callable[[str], None] = print,
        unsafe_actions: bool = False,
        start_episode: int = 1,
        episode_count: int | None = None,
        episode_retries: int = 0,
    ) -> None:
        if not specs:
            raise ValueError("specs 不能为空")
        if llm_client is None and llm_client_factory is None:
            raise ValueError("llm_client 或 llm_client_factory 必须提供一个")
        if start_episode < 1:
            raise ValueError("start_episode 必须 >= 1")
        if episode_count is not None and episode_count < 1:
            raise ValueError("episode_count 必须 >= 1")
        if episode_count is not None and episode_count > len(specs):
            raise ValueError(
                f"episode_count={episode_count} 超过 specs 数量 {len(specs)}"
            )
        if episode_retries < 0:
            raise ValueError("episode_retries 必须 >= 0")
        self.runner_class = runner_class
        self.specs = list(specs)
        self.llm_client = llm_client
        self.llm_client_factory = llm_client_factory
        self.config = config
        self.run_recorder = run_recorder
        self.server_dir = Path(server_dir)
        self.bridge_port_start = bridge_port_start
        self.benchmark_root = Path(benchmark_root)
        self.registry = registry or create_default_registry()
        self.output = output
        self.unsafe_actions = unsafe_actions
        self.start_episode = start_episode
        self.episode_count = episode_count
        self.episode_retries = episode_retries
        self._bundle_factory = bundle_factory or self._default_bundle
        self._runner_kwargs_factory = runner_kwargs_factory

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

    def run(self) -> SequentialBaselineResult:
        result = SequentialBaselineResult()
        existing = getattr(self.run_recorder, "episode_count", None)
        if existing is not None and existing != self.start_episode - 1:
            raise ValueError(
                f"run 已有 {existing} 个 episode，start_episode={self.start_episode} "
                f"要求已有 {self.start_episode - 1} 个"
            )
        count = self.episode_count or len(self.specs)
        total = self.start_episode + count - 1
        for index in range(count):
            spec = self.specs[index]
            episode_number = self.start_episode + index
            self.output(
                f"episode {episode_number}/{total}: {spec.task_id} "
                f"({self.runner_class.__name__})"
            )
            episode = self.run_recorder.start_episode(
                episode_id=str(episode_number),
                metadata={
                    "task_spec": spec.to_dict(),
                    "agents": list(spec.agents.get("names") or []),
                    "config": asdict(self.config),
                    "config_index": episode_number,
                    "retry_attempt": 1,
                },
            )
            run_result = None
            for attempt_index in range(self.episode_retries + 1):
                if attempt_index:
                    episode = self.run_recorder.retry_episode(
                        episode,
                        attempt=attempt_index,
                        metadata={
                            "task_spec": spec.to_dict(),
                            "agents": list(spec.agents.get("names") or []),
                            "config": asdict(self.config),
                            "config_index": episode_number,
                            "retry_reason": _retry_reason(run_result),
                        },
                    )
                manager = self.registry.manager_for(spec.category)()
                run_result = self._run_attempt(
                    spec=spec,
                    manager=manager,
                    episode=episode,
                    episode_number=episode_number,
                )
                if not _is_retryable_result(run_result):
                    break
                if attempt_index >= self.episode_retries:
                    if self.episode_retries > 0:
                        self.output(
                            f"episode {spec.task_id} 程序错误，已达到最大重试次数"
                        )
                    break
                self.output(
                    f"episode {spec.task_id} 程序错误，重跑当前 episode "
                    f"({attempt_index + 2}/{self.episode_retries + 1})"
                )
            result.episodes.append(
                SequentialBaselineEpisodeResult(
                    task_id=spec.task_id,
                    episode_id=episode.context.episode_id,
                    result=run_result,
                )
            )
        return result

    def _run_attempt(
        self,
        *,
        spec: TaskSpec,
        manager: Any,
        episode: Any,
        episode_number: int,
    ) -> Any:
        episode_client = None
        bundle = None
        runner = None
        run_result: Any = None
        try:
            episode_client = (
                self.llm_client_factory()
                if self.llm_client_factory is not None
                else self.llm_client
            )
            bind = getattr(episode_client, "bind_call_recorder", None)
            if callable(bind):
                bind(episode.llm_calls)
            bundle = self._bundle_factory(spec, manager, episode)
            runner_kwargs: dict[str, Any] = {}
            if self._runner_kwargs_factory is not None:
                runner_kwargs = self._runner_kwargs_factory(
                    spec, manager, episode, episode_number
                )
                if not isinstance(runner_kwargs, dict):
                    raise ValueError("runner_kwargs_factory 必须返回 dict")
            runner = self.runner_class.from_bundle(
                bundle,
                llm_client=episode_client,
                config=self.config,
                episode_recorder=episode,
                **runner_kwargs,
            )
            run_result = runner.run()
        except Exception as exc:  # noqa: BLE001
            self.output(f"episode {spec.task_id} 失败: {exc}")
            run_result = _failure_result(exc)
        finally:
            if runner is None and bundle is not None:
                close_runtime = getattr(bundle, "close_runtime", None)
                if callable(close_runtime):
                    try:
                        close_runtime()
                    except Exception as exc:  # noqa: BLE001
                        _append_cleanup_error(run_result, "bundle_runtime", exc)
            if self.llm_client_factory is not None and episode_client is not None:
                close = getattr(episode_client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:  # noqa: BLE001
                        _append_cleanup_error(run_result, "llm_client", exc)

        if run_result is None:
            run_result = _failure_result(
                RuntimeError("runner did not produce a result")
            )
        if getattr(episode, "status", None) == "started":
            try:
                episode.finalize(
                    status=(
                        "interrupted"
                        if getattr(run_result, "interrupted", False)
                        else "completed"
                        if getattr(run_result, "exit_code", 1) == 0
                        else "failed"
                    ),
                    result=run_result,
                    memory=getattr(runner, "memory", None),
                    error=getattr(run_result, "runner_error", None),
                )
            except Exception as exc:  # noqa: BLE001
                _append_cleanup_error(run_result, "recording", exc)
        return run_result


def _is_retryable_result(result: Any) -> bool:
    if getattr(result, "interrupted", False):
        return False
    if any(
        getattr(result, name, None)
        for name in ("runner_error", "worker_errors")
    ):
        return True
    return getattr(result, "outcome", None) is None


def _retry_reason(result: Any) -> str:
    if getattr(result, "runner_error", None):
        return str(result.runner_error)
    for name in ("worker_errors", "cleanup_errors"):
        errors = getattr(result, name, None)
        if errors:
            return f"{name}: {errors[0]}"
    return "missing outcome"


def _append_cleanup_error(result: Any, source: str, error: Exception) -> None:
    if result is None:
        return
    errors = getattr(result, "cleanup_errors", None)
    if isinstance(errors, list):
        errors.append((source, str(error)))


def _failure_result(exc: Exception) -> Any:
    """构造与 runner 结果结构兼容的失败结果（runner_error + exit_code 1）。"""
    return SimpleRunResult(runner_error=str(exc))


class SimpleRunResult:
    """无 runner 依赖的通用失败结果。"""

    def __init__(self, *, runner_error: str | None = None) -> None:
        self.runner_error = runner_error
        self.interrupted = False
        self.worker_errors: list[tuple[str, str]] = []
        self.cleanup_errors: list[tuple[str, str]] = []
        self.outcome = None
        self.planned_count = 0
        self.executed_count = 0
        self.stop_reason = None
        self.started_at = None
        self.finished_at = None

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        if self.runner_error or self.worker_errors or self.cleanup_errors:
            return 1
        return 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": None,
            "final_metrics": {},
            "interrupted": self.interrupted,
            "stop_reason": self.stop_reason,
            "runner_error": self.runner_error,
            "worker_errors": [list(item) for item in self.worker_errors],
            "cleanup_errors": [list(item) for item in self.cleanup_errors],
            "planned_count": self.planned_count,
            "executed_count": self.executed_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }


__all__ = [
    "SequentialBaselineEpisodeResult",
    "SequentialBaselineResult",
    "SequentialBaselineRunner",
]
