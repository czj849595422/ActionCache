"""不运行 Agent 策略的任务环境生命周期编排。"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .manager import EvaluatorContext, TaskManager, TaskOutcome, TaskStatus
from .spec import TaskSpec


@runtime_checkable
class EnvironmentAgent(Protocol):
    """环境 Runner 对 Agent 的最小接口约束。"""

    username: str

    def connect(self) -> Any:
        ...

    def disconnect(self) -> None:
        ...

    def get_raw_perception(self) -> dict[str, Any] | None:
        ...


@runtime_checkable
class EnvironmentServer(Protocol):
    """环境 Runner 对服务器的最小接口约束。"""

    @property
    def is_running(self) -> bool:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


@runtime_checkable
class EnvironmentWorkspace(Protocol):
    """环境启动前准备、服务器停止后清理的一次性工作区。"""

    def prepare(self) -> None:
        ...

    def cleanup(self) -> None:
        ...


@dataclass
class TaskEnvironmentRunResult:
    """一次纯环境运行的终态与清理结果。"""

    outcome: TaskOutcome | None = None
    interrupted: bool = False
    cleanup_errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        if self.cleanup_errors:
            return 1
        if self.outcome is not None and self.outcome.status == TaskStatus.SUCCESS:
            return 0
        return 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict() if self.outcome is not None else None,
            "interrupted": self.interrupted,
            "cleanup_errors": [list(item) for item in self.cleanup_errors],
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class TaskEnvironmentPoll:
    """一次任务轮询得到的完成状态与指标。"""

    outcome: TaskOutcome
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict(),
            "metrics": dict(self.metrics),
        }


MetricsReporter = Callable[[dict[str, Any]], None]
OutcomeReporter = Callable[[TaskOutcome], None]


class TaskEnvironmentSession:
    """可复用的纯任务环境会话。

    会话负责完整环境生命周期，但不会自行等待终态或关闭。调用方可通过
    ``poll()`` 驱动完成判定，并在需要时显式 ``close()``。首次终态会被
    缓存，后续轮询不再调用 Manager，现场一直保留到关闭会话。
    """

    def __init__(
        self,
        *,
        spec: TaskSpec,
        manager: TaskManager,
        server: EnvironmentServer,
        agents: Sequence[EnvironmentAgent],
        world_workspace: EnvironmentWorkspace | None = None,
        memory: Any = None,
        keep_server: bool = False,
        prepare_seconds: float = 10.0,
        defer_task_timing: bool = False,
        output: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not agents:
            raise ValueError("agents must not be empty")
        if prepare_seconds < 0:
            raise ValueError("prepare_seconds must be non-negative")
        if world_workspace is not None and keep_server:
            raise ValueError("keep_server cannot be used with a disposable world")
        self.spec = spec
        self.manager = manager
        self.server = server
        self.agents = list(agents)
        self.world_workspace = world_workspace
        self.memory = memory
        self.keep_server = keep_server
        self.prepare_seconds = prepare_seconds
        self.defer_task_timing = defer_task_timing
        self.output = output
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._connected: list[EnvironmentAgent] = []
        self._configured = False
        self._workspace_prepared = False
        self._started = False
        self._closed = False
        self._context: EvaluatorContext | None = None
        self._task_started_at: float | None = None
        self._terminal_outcome: TaskOutcome | None = None
        self._last_metrics: dict[str, Any] = {}
        self._cleanup_errors: list[tuple[str, str]] = []

    @property
    def context(self) -> EvaluatorContext | None:
        return self._context

    @property
    def outcome(self) -> TaskOutcome | None:
        return self._terminal_outcome

    def start(self) -> EvaluatorContext:
        """执行 configure -> 世界准备 -> server/agent -> prepare -> start。"""
        with self._lock:
            if self._started:
                raise RuntimeError("TaskEnvironmentSession can only start once")
            if self._closed:
                raise RuntimeError("TaskEnvironmentSession is already closed")
            self._started = True

            self.manager.configure(self.spec)
            self._configured = True
            self.output(
                f"任务校验通过: {self.spec.task_id}, "
                f"agents={self.spec.agents['count']}, "
                f"timeout={self.spec.completion['timeout_seconds']}s"
            )

            if self.world_workspace is not None:
                if self.server.is_running:
                    raise RuntimeError("服务器运行时不能替换任务世界")
                self.world_workspace.prepare()
                self._workspace_prepared = True
                source = getattr(self.world_workspace, "source_world", "unknown")
                self.output(f"任务世界副本已准备: {source}")

            if not self.server.is_running:
                self.server.start()
            self._apply_environment_gamerules()
            for agent in self.agents:
                agent.connect()
                self._connected.append(agent)
                bridge_port = getattr(agent, "bridge_port", None)
                suffix = f": bridge={bridge_port}" if bridge_port is not None else ""
                self.output(f"{agent.username}{suffix}")

            self.manager.prepare(self.server, self.agents, self.memory)
            if self.prepare_seconds > 0:
                self.output(
                    f"统一准备期 {self.prepare_seconds:g}s：LLM 静默等待开始..."
                )
                time.sleep(self.prepare_seconds)
                self.output("准备期结束，召唤初始实体。")
            self.manager.spawn_initial_entities(self.server)
            self.manager.start()
            self._task_started_at = (
                None if self.defer_task_timing else self._monotonic()
            )
            by_name = {agent.username: agent for agent in self.agents}
            self._context = EvaluatorContext(
                server=self.server,
                perception=lambda name: by_name[name].get_raw_perception(),
                elapsed_seconds=self._elapsed_seconds,
            )
            return self._context

    def start_task_timing(self) -> None:
        """开始计算任务时间；用于将较长的环境准备排除在任务时长外。"""
        with self._lock:
            if self._context is None:
                raise RuntimeError("TaskEnvironmentSession has not started")
            if self._closed:
                raise RuntimeError("TaskEnvironmentSession is closed")
            if self._task_started_at is None:
                self._task_started_at = self._monotonic()

    def _elapsed_seconds(self) -> float:
        started_at = self._task_started_at
        if started_at is None:
            return 0.0
        return max(0.0, self._monotonic() - started_at)

    def _apply_environment_gamerules(self) -> None:
        """尽早应用环境 gamerule（如 doMobSpawning=false），消除刷怪窗口。"""
        rules = (self.spec.environment or {}).get("gamerules") or {}
        for key, value in rules.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            try:
                self.server.send_command(f"gamerule {key} {text}")
            except Exception:  # noqa: BLE001
                # 提前设置失败不阻断启动，manager.prepare 会再次设置。
                continue

    def poll(self) -> TaskEnvironmentPoll:
        """执行一次完成判定与指标查询，首次终态后返回缓存。"""
        with self._lock:
            if self._context is None:
                raise RuntimeError("TaskEnvironmentSession has not started")
            if self._closed:
                raise RuntimeError("TaskEnvironmentSession is closed")
            if self._terminal_outcome is not None:
                return TaskEnvironmentPoll(
                    self._terminal_outcome,
                    dict(self._last_metrics),
                )
            outcome = self.manager.completion_check(self._context)
            metrics = self.manager.metrics(self._context)
            self._last_metrics = dict(metrics)
            if outcome.status != TaskStatus.RUNNING:
                self._terminal_outcome = outcome
            return TaskEnvironmentPoll(outcome, dict(metrics))

    def snapshot(self) -> dict[str, Any]:
        """返回会话白名单状态，不触发 Manager 或 Agent 查询。"""
        with self._lock:
            elapsed = None
            if self._context is not None:
                elapsed = max(0.0, float(self._context.elapsed_seconds()))
            return {
                "task_id": self.spec.task_id,
                "started": self._context is not None,
                "closed": self._closed,
                "server_running": bool(self.server.is_running),
                "connected_agents": [agent.username for agent in self._connected],
                "elapsed_seconds": elapsed,
                "outcome": (
                    self._terminal_outcome.to_dict()
                    if self._terminal_outcome is not None
                    else None
                ),
                "metrics": dict(self._last_metrics),
                "cleanup_errors": [list(item) for item in self._cleanup_errors],
            }

    def close(self) -> list[tuple[str, str]]:
        """按 Manager、Agent、Server、世界副本顺序幂等清理。"""
        with self._lock:
            if self._closed:
                return list(self._cleanup_errors)
            self._closed = True
            errors: list[tuple[str, str]] = []
            if self._configured:
                try:
                    self.manager.teardown()
                except Exception as exc:  # noqa: BLE001
                    errors.append(("task_manager", str(exc)))
            for agent in reversed(self._connected):
                try:
                    agent.disconnect()
                except Exception as exc:  # noqa: BLE001
                    errors.append((agent.username, str(exc)))
            if not self.keep_server:
                try:
                    if self.server.is_running:
                        self.server.stop()
                except Exception as exc:  # noqa: BLE001
                    errors.append(("server", str(exc)))
            if self._workspace_prepared and self.world_workspace is not None:
                try:
                    if self.server.is_running:
                        raise RuntimeError("服务器仍在运行，拒绝删除任务世界副本")
                    self.world_workspace.cleanup()
                    self._workspace_prepared = False
                except Exception as exc:  # noqa: BLE001
                    errors.append(("world_workspace", str(exc)))
            self._cleanup_errors = errors
            for name, message in errors:
                self.output(f"清理失败 [{name}]: {message}")
            return list(errors)


class TaskEnvironmentRunner:
    """仅运行任务环境，不创建 Agent 策略或动作 worker。

    正常模式在首次终态后立即清理。debug 模式在首次终态后停止轮询，
    保持 Manager、Agent 与服务器现场，直到人工 Ctrl+C 才执行清理。
    """

    def __init__(
        self,
        *,
        spec: TaskSpec,
        manager: TaskManager,
        server: EnvironmentServer,
        agents: Sequence[EnvironmentAgent],
        world_workspace: EnvironmentWorkspace | None = None,
        memory: Any = None,
        poll_interval: float = 0.2,
        debug: bool = False,
        keep_server: bool = False,
        prepare_seconds: float = 10.0,
        metrics_reporter: MetricsReporter | None = None,
        outcome_reporter: OutcomeReporter | None = None,
        output: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        debug_wait: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not agents:
            raise ValueError("agents must not be empty")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if world_workspace is not None and keep_server:
            raise ValueError("keep_server cannot be used with a disposable world")
        self.spec = spec
        self.manager = manager
        self.server = server
        self.agents = list(agents)
        self.world_workspace = world_workspace
        self.memory = memory
        self.poll_interval = poll_interval
        self.debug = debug
        self.keep_server = keep_server
        self.metrics_reporter = metrics_reporter
        self.outcome_reporter = outcome_reporter
        self.output = output
        self._sleep = sleep
        self._debug_wait = debug_wait or self._wait_for_interrupt
        self._monotonic = monotonic
        self._run_started = False
        self.session = TaskEnvironmentSession(
            spec=spec,
            manager=manager,
            server=server,
            agents=agents,
            world_workspace=world_workspace,
            memory=memory,
            keep_server=keep_server,
            prepare_seconds=prepare_seconds,
            output=output,
            monotonic=monotonic,
        )

    def run(self) -> TaskEnvironmentRunResult:
        """运行到终态；debug 模式继续保留现场直到 Ctrl+C。"""
        if self._run_started:
            raise RuntimeError("TaskEnvironmentRunner can only run once")
        self._run_started = True
        result = TaskEnvironmentRunResult()
        try:
            self._start_environment()
            result.outcome = self._wait_for_terminal()
            self._report_outcome(result.outcome)
            if self.debug:
                self.output(
                    "debug 模式已保留任务现场；按 Ctrl+C 清理 Agent、任务环境和服务器"
                )
                self._debug_wait()
        except KeyboardInterrupt:
            result.interrupted = True
            self.output("收到 Ctrl+C，开始清理任务现场")
        finally:
            result.cleanup_errors.extend(self._cleanup())
        return result

    def _start_environment(self) -> EvaluatorContext:
        return self.session.start()

    def _wait_for_terminal(self, context: EvaluatorContext | None = None) -> TaskOutcome:
        while True:
            poll = self.session.poll()
            outcome = poll.outcome
            metrics = poll.metrics
            if self.metrics_reporter is not None:
                self.metrics_reporter(metrics)
            if outcome.status != TaskStatus.RUNNING:
                return outcome
            self._sleep(self.poll_interval)

    def _report_outcome(self, outcome: TaskOutcome) -> None:
        if self.outcome_reporter is not None:
            self.outcome_reporter(outcome)
        else:
            self.output(str(outcome.to_dict()))

    def _wait_for_interrupt(self) -> None:
        while True:
            self._sleep(1.0)

    def _cleanup(self) -> list[tuple[str, str]]:
        return self.session.close()
