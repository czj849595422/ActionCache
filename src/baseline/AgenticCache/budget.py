"""Small LLM-call budget helper local to AgenticCache."""

from __future__ import annotations

import threading


class RunBudget:
    """Thread-safe team/per-agent LLM call limiter."""

    def __init__(
        self,
        *,
        team_cap: int | None = None,
        per_agent_cap: int | None = None,
    ) -> None:
        self.team_cap = team_cap
        self.per_agent_cap = per_agent_cap
        self._team_used = 0
        self._per_agent_used: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, agent_id: str) -> bool:
        with self._lock:
            if not self._can_acquire_locked(agent_id):
                return False
            used = self._per_agent_used.get(agent_id, 0)
            self._team_used += 1
            self._per_agent_used[agent_id] = used + 1
            return True

    def can_acquire(self, agent_id: str) -> bool:
        with self._lock:
            return self._can_acquire_locked(agent_id)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "team_cap": self.team_cap,
                "team_used": self._team_used,
                "per_agent_cap": self.per_agent_cap,
                "per_agent_used": dict(self._per_agent_used),
            }

    def _can_acquire_locked(self, agent_id: str) -> bool:
        if self.team_cap is not None and self._team_used >= self.team_cap:
            return False
        used = self._per_agent_used.get(agent_id, 0)
        return not (self.per_agent_cap is not None and used >= self.per_agent_cap)


__all__ = ["RunBudget"]
