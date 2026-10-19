"""Private A2A state and audit store for the AutoGen baseline."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .schemas import (
    A2AMessage,
    AgentSubtaskState,
    SubtaskAssignment,
    SubtaskReport,
)


EventSink = Callable[..., None]


class A2AStore:
    """Thread-safe private Commander/executor communication store."""

    def __init__(
        self,
        *,
        commander_id: str = "Commander",
        agents: Iterable[str] = (),
        event_sink: EventSink | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.commander_id = commander_id
        self._messages: list[A2AMessage] = []
        self._states: dict[str, AgentSubtaskState] = {}
        self._version = 0
        self._lock = threading.RLock()
        self._event_sink = event_sink
        self._audit_path = Path(audit_path) if audit_path is not None else None
        if self._audit_path is not None:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_agents(agents)

    def ensure_agents(self, agents: Iterable[str]) -> None:
        with self._lock:
            for agent_id in agents:
                if agent_id not in self._states:
                    self._states[agent_id] = AgentSubtaskState(agent_id=agent_id)

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def message_count(self) -> int:
        with self._lock:
            return len(self._messages)

    def eligible_recipients(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                agent_id
                for agent_id, state in self._states.items()
                if state.is_available_for_assignment
            )

    def state(self, agent_id: str) -> AgentSubtaskState:
        with self._lock:
            self.ensure_agents((agent_id,))
            return self._copy_state(self._states[agent_id])

    def states(self) -> dict[str, AgentSubtaskState]:
        with self._lock:
            return {
                agent_id: self._copy_state(state)
                for agent_id, state in self._states.items()
            }

    def active_assignment(self, agent_id: str) -> SubtaskAssignment | None:
        with self._lock:
            state = self._states.get(agent_id)
            return state.active_assignment if state is not None else None

    def assign(self, assignment: SubtaskAssignment) -> A2AMessage:
        with self._lock:
            state = self._states.get(assignment.recipient)
            if state is None:
                raise ValueError(f"unknown assignment recipient: {assignment.recipient}")
            if not state.is_available_for_assignment:
                raise ValueError(
                    f"recipient is not available for assignment: {assignment.recipient}"
                )
            state.active_assignment = assignment
            state.status = "assigned"
            state.planning_round = 0
            state.updated_at = time.time()
            message = A2AMessage(
                sender=self.commander_id,
                recipient=assignment.recipient,
                message_type="assignment",
                content=assignment.to_dict(),
            )
            self._append_locked(message)
            return message

    def mark_working(self, agent_id: str) -> None:
        with self._lock:
            state = self._states[agent_id]
            if state.active_assignment is None:
                return
            state.status = "working"
            state.updated_at = time.time()

    def increment_planning_round(self, agent_id: str) -> int:
        with self._lock:
            state = self._states[agent_id]
            state.planning_round += 1
            state.updated_at = time.time()
            return state.planning_round

    def submit_report(self, report: SubtaskReport) -> A2AMessage:
        with self._lock:
            state = self._states.get(report.sender)
            if state is None:
                raise ValueError(f"unknown report sender: {report.sender}")
            state.active_assignment = None
            state.status = "reported_waiting"
            state.planning_round = 0
            state.last_report = report
            state.updated_at = time.time()
            message = A2AMessage(
                sender=report.sender,
                recipient=self.commander_id,
                message_type="report",
                content=report.to_dict(),
            )
            self._append_locked(message)
            return message

    def commander_history(self) -> list[A2AMessage]:
        with self._lock:
            return list(self._messages)

    def executor_history(
        self,
        agent_id: str,
        *,
        limit: int | None = None,
    ) -> list[A2AMessage]:
        with self._lock:
            items = [
                message
                for message in self._messages
                if (
                    message.sender == self.commander_id
                    and message.recipient == agent_id
                )
                or (
                    message.sender == agent_id
                    and message.recipient == self.commander_id
                )
            ]
            if limit is not None:
                items = items[-limit:]
            return list(items)

    def _append_locked(self, message: A2AMessage) -> None:
        self._messages.append(message)
        self._version += 1
        payload = message.to_dict()
        if self._audit_path is not None:
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if self._event_sink is not None:
            self._event_sink("a2a.message", **payload)

    @staticmethod
    def _copy_state(state: AgentSubtaskState) -> AgentSubtaskState:
        return AgentSubtaskState(
            agent_id=state.agent_id,
            status=state.status,
            active_assignment=state.active_assignment,
            planning_round=state.planning_round,
            last_report=state.last_report,
            updated_at=state.updated_at,
        )


__all__ = ["A2AStore"]
