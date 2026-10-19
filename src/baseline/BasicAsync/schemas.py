"""BasicAsync：最普通异步规划 baseline 的配置与结果契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BasicAsyncConfig:
    """运行时配置：无缓存/并行/定时规划相关项。"""

    language: str = "zh"
    observation_interval: float = 1.0
    acting_poll_interval: float = 0.2
    task_poll_interval: float = 0.2
    initial_observation_timeout: float = 10.0
    worker_stop_timeout: float = 5.0
    chat_window: int | None = 20
    max_run_seconds: float | None = None
    max_llm_calls: int | None = None
    max_llm_calls_per_agent: int | None = None
    max_consecutive_errors: int = 3
    broadcast_results: bool = True
    enable_chat_actions: bool = False
    system_messages_enabled: bool = False
    system_message_interval: float = 30.0

    def __post_init__(self) -> None:
        if self.language not in {"zh", "en"}:
            raise ValueError("language 必须是 'zh' 或 'en'")
        for name in (
            "observation_interval",
            "acting_poll_interval",
            "task_poll_interval",
            "initial_observation_timeout",
            "worker_stop_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正数")
        if self.chat_window is not None and self.chat_window < 1:
            raise ValueError("chat_window 必须为正数")
        if self.max_run_seconds is not None and self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds 必须为正数")
        if self.max_llm_calls is not None and self.max_llm_calls < 1:
            raise ValueError("max_llm_calls 必须 >= 1")
        if (
            self.max_llm_calls_per_agent is not None
            and self.max_llm_calls_per_agent < 1
        ):
            raise ValueError("max_llm_calls_per_agent 必须 >= 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors 必须 >= 1")
        if not isinstance(self.broadcast_results, bool):
            raise ValueError("broadcast_results 必须是布尔值")
        if not isinstance(self.enable_chat_actions, bool):
            raise ValueError("enable_chat_actions 必须是布尔值")
        if not isinstance(self.system_messages_enabled, bool):
            raise ValueError("system_messages_enabled 必须是布尔值")
        if (
            isinstance(self.system_message_interval, bool)
            or not isinstance(self.system_message_interval, (int, float))
            or self.system_message_interval <= 0
        ):
            raise ValueError("system_message_interval 必须为正数")


@dataclass
class BasicAsyncRunResult:
    """一次 BasicAsyncRunner 调用的终态结果。"""

    outcome: Any | None = None
    final_metrics: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    stop_reason: str | None = None
    runner_error: str | None = None
    worker_errors: list[tuple[str, str]] = field(default_factory=list)
    cleanup_errors: list[tuple[str, str]] = field(default_factory=list)
    planned_count: int = 0
    executed_count: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def exit_code(self) -> int:
        from src.tasks import TaskStatus

        if self.interrupted:
            return 130
        if self.runner_error or self.worker_errors or self.cleanup_errors:
            return 1
        if self.outcome is None or self.outcome.status != TaskStatus.SUCCESS:
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "final_metrics": dict(self.final_metrics),
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


__all__ = ["BasicAsyncConfig", "BasicAsyncRunResult"]
