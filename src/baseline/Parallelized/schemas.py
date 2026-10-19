"""Shared schemas for the text-based PPA baseline runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any
from uuid import uuid4

from src.interface import ActionUnit
from src.tasks import TaskOutcome, TaskStatus


@dataclass(frozen=True)
class PPAConfig:
    """Runtime timing and retry policy for one PPA run."""

    language: str = "zh"
    observation_interval: float = 1.0
    planning_interval: float = 0.0
    acting_poll_interval: float = 0.2
    task_poll_interval: float = 0.2
    initial_observation_timeout: float = 10.0
    worker_stop_timeout: float = 5.0
    max_consecutive_errors: int = 3
    chat_window: int | None = 20
    max_run_seconds: float | None = None
    max_llm_calls: int | None = None
    max_llm_calls_per_agent: int | None = None
    simple_instructions: bool = True
    broadcast_results: bool = True
    system_messages_enabled: bool = False
    system_message_interval: float = 30.0

    def __post_init__(self) -> None:
        if self.language not in {"zh", "en"}:
            raise ValueError("language must be 'zh' or 'en'")
        for name in (
            "observation_interval",
            "acting_poll_interval",
            "task_poll_interval",
            "initial_observation_timeout",
            "worker_stop_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.planning_interval < 0:
            raise ValueError("planning_interval must not be negative")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be positive")
        if self.chat_window is not None and self.chat_window < 1:
            raise ValueError("chat_window must be positive when provided")
        if self.max_run_seconds is not None and self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive when provided")
        if self.max_llm_calls is not None and self.max_llm_calls < 1:
            raise ValueError("max_llm_calls must be positive when provided")
        if (
            self.max_llm_calls_per_agent is not None
            and self.max_llm_calls_per_agent < 1
        ):
            raise ValueError(
                "max_llm_calls_per_agent must be positive when provided"
            )
        if not isinstance(self.simple_instructions, bool):
            raise ValueError("simple_instructions must be a boolean")
        if not isinstance(self.broadcast_results, bool):
            raise ValueError("broadcast_results must be a boolean")
        if not isinstance(self.system_messages_enabled, bool):
            raise ValueError("system_messages_enabled must be a boolean")
        if (
            isinstance(self.system_message_interval, bool)
            or not isinstance(self.system_message_interval, (int, float))
            or self.system_message_interval <= 0
        ):
            raise ValueError("system_message_interval must be positive")


@dataclass
class PPAProposal:
    """One validated planning decision ready for the acting worker."""

    action_unit: ActionUnit
    interrupt: bool
    reason: str
    passive_message: str | None
    raw_text: str
    plan_id: str = field(default_factory=lambda: f"plan-{uuid4().hex[:12]}")
    planned_at: float = field(default_factory=time.time)
    memory_entry_id: str | None = None
    request_id: str | None = None
    input_params: dict[str, Any] = field(default_factory=dict)
    input_positional: list[Any] = field(default_factory=list)
    planning_started_at: float | None = None
    planning_finished_at: float | None = None


@dataclass
class PPARunResult:
    """Terminal result of a PPARunner invocation."""

    outcome: TaskOutcome | None = None
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
