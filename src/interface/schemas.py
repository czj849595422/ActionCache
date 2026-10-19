"""
Shared protocol schemas for module-to-module interaction.

The classes in this module are intentionally lightweight dataclasses. Runtime
modules can keep their existing APIs while exposing adapters that accept or
return these protocol objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any
from uuid import uuid4


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class SerializableSchema:
    """Small serialization helper used by interface dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if "self_state" in data:
            data["self"] = data.pop("self_state")
        return data


@dataclass
class AgentIdentity(SerializableSchema):
    agent_id: str
    username: str
    role: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_agent(cls, agent: Any, *, role: str | None = None) -> "AgentIdentity":
        username = str(getattr(agent, "username", ""))
        return cls(
            agent_id=username,
            username=username,
            role=role if role is not None else getattr(agent, "role", None),
            metadata={
                "host": getattr(agent, "host", None),
                "port": getattr(agent, "port", None),
                "bridge_port": getattr(agent, "bridge_port", None),
                "auth": getattr(agent, "auth", None),
                "version": getattr(agent, "version", None),
                "view_distance": getattr(agent, "view_distance", None),
                "main_hand": getattr(agent, "main_hand", None),
            },
        )


@dataclass
class PerceptionSnapshot(SerializableSchema):
    snapshot_id: str
    agent_id: str | None
    mode: str
    sub_mode: str
    timestamp: float
    self_state: dict[str, Any] = field(default_factory=dict)
    entities: list[dict[str, Any]] = field(default_factory=list)
    players: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        agent_id: str | None,
        mode: str,
        sub_mode: str,
        include_raw: bool = True,
    ) -> "PerceptionSnapshot":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            snapshot_id=_new_id("per"),
            agent_id=agent_id,
            mode=mode,
            sub_mode=sub_mode,
            timestamp=time.time(),
            self_state=dict(data.get("self") or {}),
            entities=list(data.get("entities") or []),
            players=list(data.get("players") or []),
            items=list(data.get("items") or data.get("droppedItems") or []),
            blocks=list(data.get("blocks") or []),
            metadata=dict(data.get("metadata") or {}),
            raw=data if include_raw else None,
        )


@dataclass
class Report(SerializableSchema):
    report_id: str
    agent_id: str
    kind: str
    content: dict[str, Any]
    priority: int = 0
    snapshot_id: str | None = None
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        kind: str,
        content: dict[str, Any],
        priority: int = 0,
        snapshot_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Report":
        return cls(
            report_id=_new_id("rep"),
            agent_id=agent_id,
            kind=kind,
            content=content,
            priority=priority,
            snapshot_id=snapshot_id,
            metadata=metadata or {},
        )


@dataclass
class Plan(SerializableSchema):
    plan_id: str
    objective: str
    predicted_state: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    assigned_agents: list[str] | None = None
    status: str = "proposed"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionUnit(SerializableSchema):
    agent_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    mode: str = "oneshot"
    action_id: str | None = None
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    stop_conditions: list[dict[str, Any]] = field(default_factory=list)
    interruptible: bool = True
    perception_rule: Any = None
    timeout: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionUnit":
        return cls(
            action_id=data.get("action_id") or data.get("actionId"),
            agent_id=str(data.get("agent_id") or data.get("agentId") or ""),
            action=str(data.get("action") or ""),
            mode=str(data.get("mode") or "oneshot"),
            params=dict(data.get("params") or {}),
            preconditions=list(data.get("preconditions") or []),
            stop_conditions=list(data.get("stop_conditions") or data.get("stopConditions") or []),
            interruptible=bool(data.get("interruptible", True)),
            perception_rule=data.get("perception_rule") or data.get("perceptionRule"),
            timeout=data.get("timeout"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ActionResultEnvelope(SerializableSchema):
    action: str
    ok: bool
    agent_id: str | None = None
    action_id: str | None = None
    status: str | None = None
    result: Any = None
    error: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    raw: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_planner_result(
        cls,
        result: Any,
        *,
        agent_id: str | None = None,
        requested_action_id: str | None = None,
    ) -> "ActionResultEnvelope":
        return cls(
            action=str(getattr(result, "action", "")),
            ok=bool(getattr(result, "ok", False)),
            agent_id=agent_id,
            action_id=getattr(result, "action_id", None) or requested_action_id,
            status=getattr(result, "status", None),
            result=getattr(result, "result", None),
            error=getattr(result, "error", None),
            raw=getattr(result, "raw", None),
            metadata={"requested_action_id": requested_action_id},
        )


@dataclass
class SessionTrace(SerializableSchema):
    session_id: str
    role: str
    prefix_hash: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMSemanticUnit(SerializableSchema):
    type: str
    content: dict[str, Any] = field(default_factory=dict)
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    plan_id: str | None = None
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMSemanticUnit":
        content = dict(data.get("content") or {})
        params = data.get("params", content.get("params", {}))
        if params is None:
            params = {}
        if not isinstance(params, dict):
            params = {"value": params}
        return cls(
            type=str(data.get("type") or content.get("type") or ""),
            content=content,
            action=data.get("action") or content.get("action"),
            params=dict(params),
            plan_id=data.get("plan_id") or data.get("planId") or content.get("plan_id"),
            reason=data.get("reason") or content.get("reason"),
            raw=dict(data),
        )
