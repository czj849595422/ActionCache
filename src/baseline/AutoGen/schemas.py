"""Data contracts for the AutoGen-style hierarchical A2A baseline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4
import time

from src.baseline.BasicAsync.schemas import BasicAsyncConfig, BasicAsyncRunResult


TERMINAL_SUBTASK_STATUSES = frozenset({"completed", "failed", "blocked", "max_rounds"})


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class AutoGenConfig(BasicAsyncConfig):
    """Runtime configuration for the hierarchical A2A baseline."""

    commander_id: str = "Commander"
    default_subtask_max_rounds: int = 5
    max_active_subtasks_per_agent: int = 1
    commander_interval: float = 0.5
    private_thread_window: int = 20
    public_result_window: int = 20
    max_consecutive_errors: int = 3
    max_consecutive_planning_errors: int = 3
    minecraft_say_results: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.commander_id:
            raise ValueError("commander_id must not be empty")
        if self.default_subtask_max_rounds < 1:
            raise ValueError("default_subtask_max_rounds must be >= 1")
        if self.max_active_subtasks_per_agent != 1:
            raise ValueError("v1 supports exactly one active subtask per agent")
        if self.commander_interval <= 0:
            raise ValueError("commander_interval must be positive")
        if self.private_thread_window < 1:
            raise ValueError("private_thread_window must be >= 1")
        if self.public_result_window < 1:
            raise ValueError("public_result_window must be >= 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")
        if self.max_consecutive_planning_errors < 1:
            raise ValueError("max_consecutive_planning_errors must be >= 1")
        if not isinstance(self.minecraft_say_results, bool):
            raise ValueError("minecraft_say_results must be a boolean")


@dataclass
class AutoGenRunResult(BasicAsyncRunResult):
    """Terminal result with AutoGen-specific counters."""

    commander_planned_count: int = 0
    subtask_report_count: int = 0
    a2a_message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            {
                "commander_planned_count": self.commander_planned_count,
                "subtask_report_count": self.subtask_report_count,
                "a2a_message_count": self.a2a_message_count,
            }
        )
        return data


@dataclass(frozen=True)
class SubtaskAssignment:
    """Commander-to-executor short-horizon work item."""

    recipient: str
    subtask_id: str
    objective: str
    success_criteria: str
    constraints: tuple[str, ...] = ()
    max_planning_rounds: int = 5
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.recipient:
            raise ValueError("assignment recipient must not be empty")
        if not self.subtask_id:
            raise ValueError("assignment subtask_id must not be empty")
        if not self.objective:
            raise ValueError("assignment objective must not be empty")
        if not self.success_criteria:
            raise ValueError("assignment success_criteria must not be empty")
        if self.max_planning_rounds < 1:
            raise ValueError("assignment max_planning_rounds must be >= 1")

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        default_max_rounds: int,
    ) -> "SubtaskAssignment":
        constraints = value.get("constraints") or ()
        if isinstance(constraints, str):
            constraints = (constraints,)
        if not isinstance(constraints, (list, tuple)):
            raise ValueError("assignment constraints must be a list or string")
        rounds = value.get("max_planning_rounds", default_max_rounds)
        return cls(
            recipient=str(value.get("recipient") or "").strip(),
            subtask_id=str(value.get("subtask_id") or "").strip(),
            objective=str(value.get("objective") or "").strip(),
            success_criteria=str(value.get("success_criteria") or "").strip(),
            constraints=tuple(str(item).strip() for item in constraints if str(item).strip()),
            max_planning_rounds=int(rounds),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipient": self.recipient,
            "subtask_id": self.subtask_id,
            "objective": self.objective,
            "success_criteria": self.success_criteria,
            "constraints": list(self.constraints),
            "max_planning_rounds": self.max_planning_rounds,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SubtaskReport:
    """Executor-to-Commander private terminal report."""

    sender: str
    subtask_id: str
    status: str
    report: str
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sender:
            raise ValueError("report sender must not be empty")
        if not self.subtask_id:
            raise ValueError("report subtask_id must not be empty")
        if self.status not in TERMINAL_SUBTASK_STATUSES:
            raise ValueError(f"invalid subtask report status: {self.status!r}")
        if not self.report:
            raise ValueError("report text must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "subtask_id": self.subtask_id,
            "status": self.status,
            "report": self.report,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class A2AMessage:
    """Private A2A message audited by the AutoGen baseline."""

    sender: str
    recipient: str
    message_type: str
    content: dict[str, Any]
    message_id: str = field(default_factory=lambda: _new_id("a2a"))
    created_at: float = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.message_type not in {"assignment", "report"}:
            raise ValueError("message_type must be assignment or report")
        if not self.sender or not self.recipient:
            raise ValueError("A2A sender and recipient must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "created_at": self.created_at,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type,
            "content": dict(self.content),
        }


@dataclass
class AgentSubtaskState:
    """Current assignment state for one executor."""

    agent_id: str
    status: str = "idle"
    active_assignment: SubtaskAssignment | None = None
    planning_round: int = 0
    last_report: SubtaskReport | None = None
    updated_at: float = field(default_factory=_now)

    @property
    def is_available_for_assignment(self) -> bool:
        return self.active_assignment is None and self.status in {
            "idle",
            "reported_waiting",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "planning_round": self.planning_round,
            "active_assignment": (
                self.active_assignment.to_dict() if self.active_assignment else None
            ),
            "last_report": self.last_report.to_dict() if self.last_report else None,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ExecutorDecision:
    """Parsed executor response for one subtask planning turn."""

    status: str
    reason: str
    action_text: str = ""
    report: str = ""
    raw_text: str = ""


__all__ = [
    "A2AMessage",
    "AgentSubtaskState",
    "AutoGenConfig",
    "AutoGenRunResult",
    "ExecutorDecision",
    "SubtaskAssignment",
    "SubtaskReport",
    "TERMINAL_SUBTASK_STATUSES",
]
