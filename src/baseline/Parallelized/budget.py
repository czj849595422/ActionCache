"""Team-wide and per-agent LLM call budgets for safe PPA runs."""

from __future__ import annotations

import threading


class RunBudget:
    """Thread-safe LLM call budget shared by all planning workers.

    Counts attempted calls (the worker checks before every model request),
    so failures and timeouts consume budget as well.  ``exhausted`` becomes
    true once the team cap is reached or any single agent reaches its own
    cap; the runner treats that as a stop reason instead of an error.
    """

    def __init__(
        self,
        *,
        team_cap: int | None = None,
        per_agent_cap: int | None = None,
    ) -> None:
        if team_cap is not None and team_cap < 1:
            raise ValueError("team_cap must be positive when provided")
        if per_agent_cap is not None and per_agent_cap < 1:
            raise ValueError("per_agent_cap must be positive when provided")
        self.team_cap = team_cap
        self.per_agent_cap = per_agent_cap
        self._lock = threading.RLock()
        self._team_used = 0
        self._agent_used: dict[str, int] = {}

    def try_acquire(self, agent_id: str) -> bool:
        """Atomically reserve one call for ``agent_id`` when under every cap."""
        with self._lock:
            if self.team_cap is not None and self._team_used >= self.team_cap:
                return False
            if (
                self.per_agent_cap is not None
                and self._agent_used.get(agent_id, 0) >= self.per_agent_cap
            ):
                return False
            self._team_used += 1
            self._agent_used[agent_id] = self._agent_used.get(agent_id, 0) + 1
            return True

    @property
    def team_used(self) -> int:
        with self._lock:
            return self._team_used

    def used_by_agent(self, agent_id: str) -> int:
        with self._lock:
            return self._agent_used.get(agent_id, 0)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            if self.team_cap is not None and self._team_used >= self.team_cap:
                return True
            if self.per_agent_cap is not None:
                return any(
                    used >= self.per_agent_cap
                    for used in self._agent_used.values()
                )
            return False


__all__ = ["RunBudget"]
