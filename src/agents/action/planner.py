"""
High-level action planner.
"""

from __future__ import annotations

import time
from typing import Any, Iterable
from uuid import uuid4

import requests

from src.interface import ActionResultEnvelope, ActionUnit

from .conflict import ConflictManager
from .registry import SkillCommandSpec, SkillRegistry
from .schemas import ActionMode, ActionRequest, ActionResult, ActionStatus


class BaseActionPlanner:
    """
    Dispatch high-level actions to skill runtimes.

    The planner stays runtime-agnostic: Python handles manifests, conflict
    checks, and lifecycle bookkeeping; bridge.js executes Mineflayer skills.
    """

    def __init__(
        self,
        agent: Any,
        *,
        perception: Any = None,
        registry: SkillRegistry | None = None,
        conflict_manager: ConflictManager | None = None,
        bridge_timeout: float = 60.0,
        allowed_actions: Iterable[str] | None = None,
        internal_actions: Iterable[str] = (),
    ):
        self.agent = agent
        self.perception = perception
        self.registry = registry or SkillRegistry(allowed_actions=allowed_actions)
        self.internal_actions = frozenset(internal_actions)
        self.conflicts = conflict_manager or ConflictManager()
        self.bridge_timeout = bridge_timeout
        self.active_actions: dict[str, dict[str, Any]] = {}
        self._fallback_session: requests.Session | None = None

    def execute(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        mode: str = ActionMode.ONESHOT,
        perception_rule: Any = None,
        timeout: float | None = None,
        interruptible: bool = True,
    ) -> ActionResult:
        """Execute an action. Continuous mode delegates to start()."""
        if mode == ActionMode.CONTINUOUS:
            return self.start(
                action,
                params=params,
                perception_rule=perception_rule,
                timeout=timeout,
                interruptible=interruptible,
            )

        return self._execute_oneshot(
            action,
            params=params,
            perception_rule=perception_rule,
            timeout=timeout,
            interruptible=interruptible,
            internal=False,
        )

    def execute_internal(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> ActionResult:
        """Execute a trusted Manager-only action that is hidden from task agents."""
        if action not in self.internal_actions:
            return ActionResult.failed(action, f"Action is not trusted for internal use: {action}")
        return self._execute_oneshot(
            action,
            params=params,
            perception_rule=None,
            timeout=timeout,
            interruptible=False,
            internal=True,
        )

    def _execute_oneshot(
        self,
        action: str,
        *,
        params: dict[str, Any] | None,
        perception_rule: Any,
        timeout: float | None,
        interruptible: bool,
        internal: bool,
    ) -> ActionResult:
        request = ActionRequest(
            action=action,
            params=params or {},
            mode=ActionMode.ONESHOT,
            perception_rule=perception_rule,
            timeout=timeout,
            interruptible=interruptible,
        )
        request.validate()
        try:
            _, spec = self._validate_request(request, internal=internal)
            action_id = self._new_action_id()
            decision = self.conflicts.acquire(action_id, action, spec)
            if not decision.allowed:
                return ActionResult.failed(action, decision.reason or "Action conflict.")
            if decision.switch_action_id:
                self.stop(decision.switch_action_id, reason="switch")

            payload = self._payload(request, action_id=action_id, internal=internal)
            data = self._post("/skill/execute", payload, timeout=timeout)
            return ActionResult.from_bridge(action, data)
        except Exception as exc:
            return ActionResult.failed(action, str(exc))
        finally:
            if "action_id" in locals():
                self.conflicts.release(action_id)

    def execute_unit(self, action_unit: ActionUnit | dict[str, Any]) -> ActionResultEnvelope:
        """Execute a normalized cross-module ActionUnit."""
        unit = self._normalize_action_unit(action_unit)
        result = self.execute(
            unit.action,
            unit.params,
            mode=unit.mode,
            perception_rule=unit.perception_rule,
            timeout=unit.timeout,
            interruptible=unit.interruptible,
        )
        return self._envelope_from_result(result, unit)

    def start(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        mode: str = ActionMode.CONTINUOUS,
        perception_rule: Any = None,
        timeout: float | None = None,
        interruptible: bool = True,
    ) -> ActionResult:
        """Start a continuous action and return its action id."""
        request = ActionRequest(
            action=action,
            params=params or {},
            mode=mode,
            perception_rule=perception_rule,
            timeout=timeout,
            interruptible=interruptible,
        )
        request.validate()
        try:
            _, spec = self._validate_request(request)
            action_id = self._new_action_id()
            decision = self.conflicts.acquire(action_id, action, spec)
            if not decision.allowed:
                return ActionResult.failed(action, decision.reason or "Action conflict.")
            if decision.switch_action_id:
                self.stop(decision.switch_action_id, reason="switch")

            payload = self._payload(request, action_id=action_id)
            data = self._post("/skill/start", payload, timeout=timeout)
            result = ActionResult.from_bridge(action, data)
            if result.ok and result.action_id:
                self.active_actions[result.action_id] = {
                    "action": action,
                    "spec": spec,
                    "started_at": time.time(),
                    "interruptible": interruptible,
                }
            else:
                self.conflicts.release(action_id)
            return result
        except Exception as exc:
            if "action_id" in locals():
                self.conflicts.release(action_id)
            return ActionResult.failed(action, str(exc))

    def start_unit(self, action_unit: ActionUnit | dict[str, Any]) -> ActionResultEnvelope:
        """Start a continuous normalized ActionUnit."""
        unit = self._normalize_action_unit(action_unit)
        result = self.start(
            unit.action,
            unit.params,
            mode=unit.mode,
            perception_rule=unit.perception_rule,
            timeout=unit.timeout,
            interruptible=unit.interruptible,
        )
        return self._envelope_from_result(result, unit)

    def update(self, action_id: str, params: dict[str, Any]) -> ActionResult:
        """Update mutable params for a running action."""
        active = self.active_actions.get(action_id)
        if not active:
            return ActionResult.failed(action_id, f"Unknown active action: {action_id}")
        spec: SkillCommandSpec = active["spec"]
        illegal = sorted(set(params) - spec.mutable_params)
        if illegal:
            return ActionResult.failed(
                active["action"],
                f"Params are not mutable for {active['action']}: {illegal}",
            )

        data = self._post(
            "/skill/update",
            {"actionId": action_id, "params": params},
            timeout=self.bridge_timeout,
        )
        return ActionResult.from_bridge(active["action"], data)

    def stop(
        self,
        action_id: str,
        *,
        reason: str = "interrupted",
        timeout: float | None = None,
    ) -> ActionResult:
        """Stop a running action."""
        active = self.active_actions.get(action_id)
        action = active["action"] if active else action_id
        try:
            data = self._post(
                "/skill/stop",
                {"actionId": action_id, "reason": reason},
                timeout=timeout or self.bridge_timeout,
            )
            return ActionResult.from_bridge(action, data)
        finally:
            self.active_actions.pop(action_id, None)
            self.conflicts.release(action_id)

    def interrupt(self, action_id: str | None = None, *, reason: str = "interrupted") -> list[ActionResult]:
        """Interrupt one running action or all running actions."""
        action_ids = [action_id] if action_id else list(self.active_actions.keys())
        return [self.stop(current_id, reason=reason) for current_id in action_ids if current_id]

    def status(self, action_id: str | None = None) -> ActionResult | dict[str, Any]:
        """Return runtime status for one action or all actions."""
        timeout = min(self.bridge_timeout, 5.0)
        if action_id:
            data = self._get(
                "/skill/status",
                params={"actionId": action_id},
                timeout=timeout,
            )
            active = self.active_actions.get(action_id)
            action = active["action"] if active else action_id
            result = ActionResult.from_bridge(action, data)
            if result.status in ActionStatus.TERMINAL:
                self.active_actions.pop(action_id, None)
                self.conflicts.release(action_id)
            return result
        payload = self._get("/skill/status", timeout=timeout)
        actions = payload.get("actions") if isinstance(payload, dict) else None
        if isinstance(actions, list):
            for item in actions:
                if not isinstance(item, dict):
                    continue
                current_action_id = item.get("actionId") or item.get("action_id")
                if not current_action_id:
                    continue
                result = ActionResult.from_bridge(
                    self.active_actions.get(current_action_id, {}).get("action") or str(item.get("action") or current_action_id),
                    item,
                )
                if result.status in ActionStatus.TERMINAL:
                    self.active_actions.pop(str(current_action_id), None)
                    self.conflicts.release(str(current_action_id))
        return payload

    def list_skills(self) -> dict[str, Any]:
        """Return local manifests plus bridge runtime capability information."""
        local = self.registry.list_skills()
        try:
            runtime = self._get("/skill/list")
        except Exception as exc:
            runtime = {"error": str(exc)}
        return {"local": local, "runtime": runtime}

    def set_perception(self, perception: Any) -> None:
        self.perception = perception

    def _validate_request(self, request: ActionRequest, *, internal: bool = False):
        manifest, spec = self.registry.resolve(
            request.action,
            enforce_allowlist=not internal,
        )
        if request.mode not in spec.modes:
            raise ValueError(
                f"Action {request.action!r} does not support mode {request.mode!r}."
            )
        return manifest, spec

    def _payload(
        self,
        request: ActionRequest,
        *,
        action_id: str,
        internal: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "actionId": action_id,
            "action": request.action,
            "params": request.params,
            "mode": request.mode,
            "timeout": request.timeout,
            "interruptible": request.interruptible,
            "internal": internal,
        }
        perception = self._collect_perception(request.perception_rule)
        if perception is not None:
            payload["perception"] = perception
        return payload

    def _collect_perception(self, rule: Any) -> Any:
        if rule is None or self.perception is None:
            return None
        old_rule = getattr(self.perception, "rule", None)
        try:
            self.perception.rule = rule
            return self.perception.perceive()
        finally:
            self.perception.rule = old_rule

    def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if hasattr(self.agent, "_post_bridge"):
            try:
                return self.agent._post_bridge(
                    path,
                    json_data=payload,
                    timeout=timeout or self.bridge_timeout,
                    raise_on_bridge_error=False,
                )
            except TypeError as exc:
                if "raise_on_bridge_error" not in str(exc):
                    raise
                return self.agent._post_bridge(
                    path,
                    json_data=payload,
                    timeout=timeout or self.bridge_timeout,
                )
        resp = self._fallback_bridge_session().post(
            f"{self.agent._get_bridge_url()}{path}",
            json=payload,
            timeout=timeout or self.bridge_timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        bridge_get = getattr(self.agent, "_get_bridge", None)
        if callable(bridge_get):
            return bridge_get(
                path,
                params=params,
                timeout=timeout or self.bridge_timeout,
            )
        resp = self._fallback_bridge_session().get(
            f"{self.agent._get_bridge_url()}{path}",
            params=params,
            timeout=timeout or self.bridge_timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _fallback_bridge_session(self) -> requests.Session:
        if self._fallback_session is None:
            self._fallback_session = requests.Session()
            self._fallback_session.trust_env = False
        return self._fallback_session

    def _new_action_id(self) -> str:
        return f"act-{uuid4().hex[:12]}"

    def _normalize_action_unit(self, action_unit: ActionUnit | dict[str, Any]) -> ActionUnit:
        if isinstance(action_unit, ActionUnit):
            return action_unit
        if isinstance(action_unit, dict):
            return ActionUnit.from_dict(action_unit)
        raise TypeError("action_unit must be ActionUnit or dict.")

    def _envelope_from_result(
        self,
        result: ActionResult,
        unit: ActionUnit,
    ) -> ActionResultEnvelope:
        return ActionResultEnvelope.from_planner_result(
            result,
            agent_id=unit.agent_id or getattr(self.agent, "username", None),
            requested_action_id=unit.action_id,
        )
