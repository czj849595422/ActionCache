"""
Conflict detection for high-level agent actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import SkillCommandSpec


@dataclass
class RunningAction:
    action_id: str
    action: str
    locks: set[str] = field(default_factory=set)
    switch_group: str | None = None
    switchable: bool = False


@dataclass
class ConflictDecision:
    allowed: bool
    reason: str | None = None
    switch_action_id: str | None = None


class ConflictManager:
    """Track active action locks for one agent."""

    def __init__(self):
        self._running: dict[str, RunningAction] = {}

    def check(self, action_id: str, action: str, spec: SkillCommandSpec) -> ConflictDecision:
        new_locks = set(spec.locks)
        for running in self._running.values():
            if not (running.locks & new_locks):
                continue

            # switch_group is descriptive; overlapping resource locks determine replacement.
            if running.switchable and spec.switchable:
                return ConflictDecision(allowed=True, switch_action_id=running.action_id)

            locks = ", ".join(sorted(running.locks & new_locks))
            return ConflictDecision(
                allowed=False,
                reason=f"Action conflicts with running action {running.action_id} on locks: {locks}",
            )
        return ConflictDecision(allowed=True)

    def acquire(self, action_id: str, action: str, spec: SkillCommandSpec) -> ConflictDecision:
        decision = self.check(action_id, action, spec)
        if not decision.allowed:
            return decision
        self._running[action_id] = RunningAction(
            action_id=action_id,
            action=action,
            locks=set(spec.locks),
            switch_group=spec.switch_group,
            switchable=spec.switchable,
        )
        return decision

    def release(self, action_id: str) -> None:
        self._running.pop(action_id, None)

    def active(self) -> dict[str, RunningAction]:
        return dict(self._running)
