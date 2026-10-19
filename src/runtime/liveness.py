"""Entity-backed agent liveness helpers.

The baseline runners use this module as a small shared gate: Minecraft-backed
agents may keep planning and mutating method state only while their entity is
alive and not in spectator mode.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


EventSink = Callable[..., None]


def mark_agent_live(agent: Any, live: bool) -> None:
    """Set a best-effort ``live`` flag on an agent-like object."""
    try:
        setattr(agent, "live", bool(live))
    except Exception:  # noqa: BLE001
        return


def mark_agents_live(agents: Sequence[Any], live: bool) -> None:
    for agent in agents:
        mark_agent_live(agent, live)


def set_agent_live_by_name(agents: Sequence[Any], name: str, live: bool) -> None:
    target = str(name)
    for agent in agents:
        if str(getattr(agent, "username", agent)) == target:
            mark_agent_live(agent, live)
            return


def agent_is_live(agent: Any) -> bool:
    """Return whether an agent should be allowed to perform entity behavior."""
    return getattr(agent, "live", True) is not False


def snapshot_live_state(snapshot: Mapping[str, Any] | None) -> bool | None:
    """Infer live state from raw perception.

    ``False`` means definitely dead or spectator. ``True`` means health/mode
    indicate an active entity. ``None`` means the snapshot did not contain
    enough self-state to decide.
    """
    if not isinstance(snapshot, Mapping):
        return None
    decided_alive = False
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, Mapping):
            continue
        game_mode = candidate.get("gameMode")
        if game_mode is None:
            game_mode = candidate.get("gamemode")
        if isinstance(game_mode, str):
            mode = game_mode.strip().lower()
            if mode == "spectator":
                return False
            if mode:
                decided_alive = True
        health = candidate.get("health")
        if isinstance(health, (int, float)):
            if float(health) <= 0:
                return False
            decided_alive = True
    return True if decided_alive else None


class EntityLivenessGate:
    """Track and query live state for entity-backed agents."""

    def __init__(
        self,
        agents: Sequence[Any] = (),
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self._agents_by_id: dict[str, Any] = {}
        self.event_sink = event_sink
        self.register(agents)

    def register(self, agents: Sequence[Any]) -> None:
        for agent in agents:
            agent_id = str(getattr(agent, "username", agent))
            if agent_id:
                self._agents_by_id[agent_id] = agent

    def __call__(self, agent_id: str) -> bool:
        return self.is_live(agent_id)

    def is_live(self, agent_id: str) -> bool:
        agent = self._agents_by_id.get(str(agent_id))
        return True if agent is None else agent_is_live(agent)

    def mark_live(self, agent_id: str, live: bool, *, reason: str | None = None) -> None:
        agent = self._agents_by_id.get(str(agent_id))
        if agent is None:
            return
        old = agent_is_live(agent)
        mark_agent_live(agent, live)
        if old != bool(live) and self.event_sink is not None:
            self.event_sink(
                "agent.liveness_changed",
                strict=False,
                agent_id=str(agent_id),
                live=bool(live),
                reason=reason,
            )

    def mark_all_live(self, live: bool, *, reason: str | None = None) -> None:
        for agent_id in tuple(self._agents_by_id):
            self.mark_live(agent_id, live, reason=reason)

    def update_from_observation(
        self,
        agent_id: str,
        observation: Mapping[str, Any] | None,
    ) -> bool:
        state = snapshot_live_state(observation)
        if state is False:
            self.mark_live(agent_id, False, reason="observation")
            return False
        return self.is_live(agent_id)

    def update_from_metrics(self, metrics: Mapping[str, Any] | None) -> None:
        if not isinstance(metrics, Mapping):
            return
        eliminated = _string_set(metrics.get("eliminated_agents"))
        for agent_id in eliminated:
            self.mark_live(agent_id, False, reason="task_metrics.eliminated_agents")
        alive = _string_set(metrics.get("alive_agents"))
        for agent_id in alive:
            if agent_id not in eliminated and self.is_live(agent_id):
                self.mark_live(agent_id, True, reason="task_metrics.alive_agents")

    def update_from_task_state(
        self,
        *,
        outcome: Any = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> None:
        self.update_from_metrics(metrics)
        reason = getattr(outcome, "reason", None)
        if reason is None and isinstance(outcome, Mapping):
            reason = outcome.get("reason")
        if reason == "all_agents_dead":
            self.mark_all_live(False, reason="task_outcome.all_agents_dead")

    def live_agent_ids(self, agent_ids: Sequence[str] | None = None) -> tuple[str, ...]:
        candidates = tuple(agent_ids) if agent_ids is not None else tuple(self._agents_by_id)
        return tuple(str(agent_id) for agent_id in candidates if self.is_live(str(agent_id)))


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (str, bytes)):
        return {str(value)}
    if isinstance(value, Mapping):
        return {str(key) for key, item in value.items() if item}
    try:
        return {str(item) for item in value}
    except TypeError:
        return set()
