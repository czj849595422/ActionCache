"""Public schemas for run evidence and LLM call attribution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RecordingError(RuntimeError):
    """Raised when required evidence cannot be recorded safely."""


@dataclass(frozen=True)
class LLMCallSource:
    """Logical source of one model request."""

    component: str = "standalone"
    operation: str = ""
    task_id: str | None = None
    agent_id: str | None = None

    def to_dict(self, *, default_operation: str | None = None) -> dict[str, str | None]:
        return {
            "component": self.component or "standalone",
            "operation": self.operation or default_operation or "unknown",
            "task_id": self.task_id,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class RunContext:
    baseline: str
    run_id: str
    path: Path
    task: str | None = None


@dataclass(frozen=True)
class EpisodeContext:
    baseline: str
    run_id: str
    episode_id: str
    path: Path
