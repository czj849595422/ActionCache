"""多 agent 生命周期编排：启动服务器/连接 agent/运行 worker/逆序清理。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .worker import LoopWorker


@runtime_checkable
class AgentLike(Protocol):
    """TeamRunner 对 agent 的最小鸭子类型约束。"""

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...


@runtime_checkable
class ServerLike(Protocol):
    """TeamRunner 对服务器的最小鸭子类型约束。"""

    @property
    def is_running(self) -> bool:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


WorkerFactory = Callable[[Any, int], Sequence[LoopWorker]]
CompletionCheck = Callable[[list[Any], list[list[LoopWorker]]], bool]


@dataclass
class TeamRunResult:
    """一次团队运行的结果汇总。"""

    started_at: float | None = None
    finished_at: float | None = None
    timed_out: bool = False
    stop_reasons: dict[str, str | None] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timed_out": self.timed_out,
            "stop_reasons": dict(self.stop_reasons),
            "errors": [list(item) for item in self.errors],
        }


class TeamRunner:
    """多 agent 编排器（单次运行：start 一次，run/stop 后不可复用）。"""

    def __init__(
        self,
        *,
        agents: Sequence[AgentLike],
        worker_factory: WorkerFactory,
        server: ServerLike | None = None,
        completion_check: CompletionCheck | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        if not agents:
            raise ValueError("agents must not be empty")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.agents = list(agents)
        self.worker_factory = worker_factory
        self.server = server
        self.completion_check = completion_check
        self.poll_interval = poll_interval
        self._workers: list[list[LoopWorker]] = []
        self._started = False
        self._started_at: float | None = None

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("TeamRunner has already been started")
        if self.server is not None and not self.server.is_running:
            self.server.start()

        connected: list[AgentLike] = []
        try:
            for agent in self.agents:
                agent.connect()
                connected.append(agent)
            for index, agent in enumerate(self.agents):
                loops = list(self.worker_factory(agent, index))
                if not loops:
                    raise ValueError(
                        f"worker_factory returned no loops for agent {index}"
                    )
                self._workers.append(loops)
                for loop in loops:
                    loop.start()
        except Exception:
            for loops in self._workers:
                for loop in loops:
                    try:
                        loop.stop(reason="startup_failed", timeout=2.0)
                    except Exception:  # noqa: BLE001
                        pass
            for agent in reversed(connected):
                try:
                    agent.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if self.server is not None and self.server.is_running:
                try:
                    self.server.stop()
                except Exception:  # noqa: BLE001
                    pass
            raise

        self._started = True
        self._started_at = time.time()

    # ------------------------------------------------------------------
    # 等待与停止
    # ------------------------------------------------------------------

    def wait(self, timeout: float | None = None) -> TeamRunResult:
        """等待完成判定或全部 worker 停止；返回结果（不负责清理）。"""
        if not self._started:
            raise RuntimeError("TeamRunner has not been started")
        result = TeamRunResult(started_at=self._started_at)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._all_workers_stopped():
                break
            if (
                self.completion_check is not None
                and self.completion_check(self.agents, self._workers)
            ):
                break
            if deadline is not None and time.monotonic() >= deadline:
                result.timed_out = True
                break
            time.sleep(self.poll_interval)
        result.finished_at = time.time()
        self._collect(result)
        return result

    def stop(
        self,
        reason: str = "stopped",
        timeout: float | None = None,
    ) -> TeamRunResult:
        """逆序清理：先停 worker，再断开 agent，最后停服务器。"""
        if not self._started:
            raise RuntimeError("TeamRunner has not been started")
        result = TeamRunResult(
            started_at=self._started_at,
            finished_at=time.time(),
        )
        for loops in reversed(self._workers):
            for loop in reversed(loops):
                try:
                    loop.stop(reason=reason, timeout=timeout)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append((loop.name, str(exc)))
        for agent in reversed(self.agents):
            try:
                agent.disconnect()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    (getattr(agent, "username", repr(agent)), str(exc))
                )
        if self.server is not None and self.server.is_running:
            try:
                self.server.stop()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(("server", str(exc)))
        self._collect(result)
        return result

    def run(self, timeout: float | None = None) -> TeamRunResult:
        """start + wait + stop 一步完成（异常路径也会清理）。"""
        self.start()
        result = self.wait(timeout=timeout)
        stop_result = self.stop(reason="run_completed", timeout=timeout)
        result.errors.extend(stop_result.errors)
        result.stop_reasons.update(stop_result.stop_reasons)
        return result

    # ------------------------------------------------------------------
    # 状态与内部
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        agents_status: list[dict[str, Any]] = []
        for index, agent in enumerate(self.agents):
            loops = (
                self._workers[index] if index < len(self._workers) else []
            )
            agents_status.append(
                {
                    "name": getattr(
                        agent,
                        "username",
                        ("Alice", "Bob", "Carol", "Dave", "Eve")[index]
                        if index < 5
                        else f"agent-{index}",
                    ),
                    "workers": [
                        {
                            "name": loop.name,
                            "running": loop.is_running,
                            "ready": loop.ready,
                            "iteration_count": loop.iteration_count,
                            "stop_reason": loop.stop_reason,
                            "last_error": (
                                str(loop.last_error)
                                if loop.last_error is not None
                                else None
                            ),
                        }
                        for loop in loops
                    ],
                }
            )
        return {"started": self._started, "agents": agents_status}

    def _all_workers_stopped(self) -> bool:
        return all(
            not loop.is_running
            for loops in self._workers
            for loop in loops
        )

    def _collect(self, result: TeamRunResult) -> None:
        for loops in self._workers:
            for loop in loops:
                result.stop_reasons[loop.name] = loop.stop_reason
                if loop.last_error is not None:
                    result.errors.append((loop.name, str(loop.last_error)))
