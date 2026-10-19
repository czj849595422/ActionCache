"""
Base perception module.

The first stage collects raw Mineflayer perception from the agent bridge. Rule
processing is optional and defaults to returning raw data unchanged.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from src.interface import PerceptionSnapshot

from .modes import LocalPerceptionMode, PerceptionMode
from .filters import filter_observer_players
from .rule import RuleContext, RuleLike, apply_rule
from .schemas import PerceptionData, PerceptionRequest


_PERCEPTION_RETRY_DELAYS = (0.2, 0.5, 1.0)


class BasePerception:
    """Collect and optionally transform raw perception data."""

    def __init__(
        self,
        agent: Any = None,
        *,
        mode: str | None = None,
        sub_mode: str = LocalPerceptionMode.RADIUS,
        radius: float | None = None,
        block_radius: float | None = None,
        fov_degrees: float = 90.0,
        rule: RuleLike | None = None,
        bridge_url: str | None = None,
        bridge_port: int | None = None,
        timeout: float = 10.0,
    ):
        self.agent = agent
        self.mode = mode or (PerceptionMode.LOCAL if radius is not None else PerceptionMode.GLOBAL)
        self.sub_mode = sub_mode
        self.radius = radius
        self.block_radius = block_radius
        self.fov_degrees = fov_degrees
        self.rule = rule
        self.bridge_url = bridge_url
        self.bridge_port = bridge_port
        self.timeout = timeout
        self._fallback_session: requests.Session | None = None
        self._validate()

    def _validate(self) -> None:
        if self.mode not in PerceptionMode.ALL:
            raise ValueError(f"Unsupported perception mode: {self.mode!r}")
        if self.sub_mode not in LocalPerceptionMode.ALL:
            raise ValueError(f"Unsupported local perception sub_mode: {self.sub_mode!r}")
        if self.radius is not None and self.radius <= 0:
            raise ValueError("radius must be positive when provided.")
        if self.block_radius is not None and self.block_radius <= 0:
            raise ValueError("block_radius must be positive when provided.")
        if self.fov_degrees <= 0 or self.fov_degrees > 360:
            raise ValueError("fov_degrees must be in (0, 360].")

    def perceive(self) -> PerceptionData:
        """Collect raw perception and apply the configured rule if present."""
        raw = self.collect_raw()
        context = self.make_context(raw)
        return apply_rule(self.rule, raw, context)

    def perceive_snapshot(self, *, include_raw: bool = True) -> PerceptionSnapshot:
        """
        Collect perception and return the cross-module snapshot schema.

        Existing callers should keep using perceive() when they expect the
        historical dict result. HCAP-facing code should use this method.
        """
        raw = self.collect_raw()
        context = self.make_context(raw)
        perceived = apply_rule(self.rule, raw, context)
        source = perceived if isinstance(perceived, dict) else raw
        snapshot = PerceptionSnapshot.from_raw(
            source,
            agent_id=getattr(self.agent, "username", None),
            mode=self.mode,
            sub_mode=self.sub_mode,
            include_raw=include_raw,
        )
        if include_raw:
            snapshot.raw = raw
        return snapshot

    def collect_raw(self) -> PerceptionData:
        """Collect raw perception from the Mineflayer bridge."""
        request = self.make_request()
        params = {
            "mode": request.mode,
            "subMode": request.sub_mode,
            "fovDegrees": request.fov_degrees,
        }
        if request.radius is not None:
            params["radius"] = request.radius
        if request.block_radius is not None:
            params["blockRadius"] = request.block_radius

        data = self._collect_raw_with_retry(params)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(data["error"])
        return filter_observer_players(data)

    def _collect_raw_with_retry(self, params: dict[str, Any]) -> Any:
        bridge_get = getattr(self.agent, "_get_bridge", None)
        attempts = len(_PERCEPTION_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                if callable(bridge_get):
                    return bridge_get(
                        "/perception/raw",
                        params=params,
                        timeout=self.timeout,
                    )
                resp = self._fallback_bridge_session().get(
                    f"{self._get_bridge_url()}/perception/raw",
                    params=params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                if attempt >= len(_PERCEPTION_RETRY_DELAYS) or not _is_transient_bridge_error(exc):
                    raise
                time.sleep(_PERCEPTION_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")

    def make_request(self) -> PerceptionRequest:
        return PerceptionRequest(
            mode=self.mode,
            sub_mode=self.sub_mode,
            radius=self.radius,
            block_radius=self.block_radius,
            fov_degrees=self.fov_degrees,
        )

    def make_context(self, raw: PerceptionData) -> RuleContext:
        return RuleContext(
            mode=self.mode,
            sub_mode=self.sub_mode,
            radius=self.radius,
            agent_name=getattr(self.agent, "username", None),
            metadata={
                "raw_data": raw,
                "raw_metadata": raw.get("metadata", {}) if isinstance(raw, dict) else {},
            },
        )

    def _get_bridge_url(self) -> str:
        if self.bridge_url:
            return self.bridge_url.rstrip("/")
        if self.agent is not None and hasattr(self.agent, "_get_bridge_url"):
            return self.agent._get_bridge_url()
        if self.bridge_port is not None:
            return f"http://localhost:{self.bridge_port}"
        raise RuntimeError("BasePerception requires agent, bridge_url, or bridge_port.")

    def _fallback_bridge_session(self) -> requests.Session:
        if self._fallback_session is None:
            self._fallback_session = requests.Session()
            self._fallback_session.trust_env = False
        return self._fallback_session


def _is_transient_bridge_error(error: requests.RequestException) -> bool:
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    if not isinstance(error, requests.HTTPError) or error.response is None:
        return False
    status = error.response.status_code
    if status in {502, 503, 504}:
        return True
    if status != 400:
        return False
    try:
        payload = error.response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("error") == "Bot not started or not spawned"
    )
