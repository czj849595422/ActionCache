"""
Shared schemas for high-level action execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ActionMode:
    """Supported action execution modes."""

    ONESHOT = "oneshot"
    CONTINUOUS = "continuous"

    ALL = {ONESHOT, CONTINUOUS}


class ActionStatus:
    """Normalized action lifecycle statuses."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

    TERMINAL = {COMPLETED, STOPPED, INTERRUPTED, FAILED}


@dataclass
class ActionRequest:
    """High-level action request accepted by the planner."""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    mode: str = ActionMode.ONESHOT
    perception_rule: Any = None
    timeout: float | None = None
    interruptible: bool = True

    def validate(self) -> None:
        if not self.action or not isinstance(self.action, str):
            raise ValueError("action must be a non-empty string.")
        if self.mode not in ActionMode.ALL:
            raise ValueError(f"Unsupported action mode: {self.mode!r}")
        if not isinstance(self.params, dict):
            raise ValueError("params must be a dict.")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive when provided.")


@dataclass
class ActionResult:
    """Normalized result returned by planner operations."""

    action: str
    ok: bool
    action_id: str | None = None
    status: str | None = None
    result: Any = None
    error: str | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def from_bridge(cls, action: str, data: dict[str, Any]) -> "ActionResult":
        status = data.get("status")
        error = data.get("error")
        result = data.get("result")
        nested_ok = result.get("ok") if isinstance(result, dict) and "ok" in result else None
        ok = (
            bool(nested_ok)
            if nested_ok is not None
            else error is None and status not in {ActionStatus.FAILED}
        ) and error is None
        if not ok:
            status = ActionStatus.FAILED
            if error is None and isinstance(result, dict):
                error = result.get("error_code") or result.get("reason")
        return cls(
            action=action,
            ok=ok,
            action_id=data.get("actionId") or data.get("action_id"),
            status=status,
            result=result,
            error=error,
            raw=data,
        )

    @classmethod
    def failed(cls, action: str, error: str) -> "ActionResult":
        return cls(action=action, ok=False, status=ActionStatus.FAILED, error=error)
