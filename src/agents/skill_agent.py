#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skill 驱动的单 agent 管理类。

每个 SkillAgent 实例独立启动一个 bridge_skill.js 子进程（独占 BRIDGE_PORT），
通过 HTTP 调用 /skill/* 端点执行动作，并通过 BaseActionPlanner 统一管理
动作生命周期（execute/start/update/stop/interrupt/status）。

多个 agent 各自 new 一个 SkillAgent 并分配不同 bridge_port，即可做到
"每个 agent 一个进程、各自独立控制"。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from src.launcher.utils import ManagedProcess
from src.perception.filters import filter_observer_players


class SkillAgent:
    """管理单个 Mineflayer skill bridge 的生命周期与动作规划。"""

    def __init__(
        self,
        username: str = "test_agent",
        host: str = "localhost",
        port: int = 25565,
        bridge_port: int = 3200,
        bridge_js: str | Path | None = None,
        log_dir: str | Path | None = None,
        bridge_env: dict[str, str] | None = None,
        request_timeout: float = 35.0,
        allowed_actions: Iterable[str] | None = None,
        internal_actions: Iterable[str] = (),
    ):
        self.username = username
        self.host = host
        self.port = port
        self.bridge_port = bridge_port
        self.request_timeout = request_timeout
        if bridge_js is None:
            bridge_js = (
                Path(__file__).resolve().parent.parent
                / "env" / "mineflayer" / "bridge_skill.js"
            )
        self.bridge_js = Path(bridge_js)
        self.log_dir = log_dir
        allowed = None if allowed_actions is None else frozenset(allowed_actions)
        internal = frozenset(internal_actions)
        self.bridge_env = dict(bridge_env or {})
        self.bridge_env["SKILL_ACTION_ALLOWLIST"] = (
            "" if allowed is None else json.dumps(sorted(allowed))
        )
        self.bridge_env["SKILL_INTERNAL_ACTIONS"] = json.dumps(sorted(internal))
        self._monitor: ManagedProcess | None = None
        self._connected = False
        self._closing = False
        self._connect_payload: dict[str, Any] | None = None
        # Control requests and perception requests must not wait on each other.
        self._bridge_control_lock = threading.RLock()
        self._bridge_io_lock = self._bridge_control_lock
        self._bridge_perception_lock = threading.RLock()
        self.live = True
        self._bridge_session = requests.Session()
        self._bridge_session.trust_env = False
        self._perception_bridge_session = requests.Session()
        self._perception_bridge_session.trust_env = False

        from src.agents.action import BaseActionPlanner, SkillRegistry

        self.action_planner = BaseActionPlanner(
            agent=self,
            allowed_actions=allowed,
            internal_actions=internal,
        )
        self._registry = self.action_planner.registry

    # ------------------------------------------------------------------
    # Bridge HTTP
    # ------------------------------------------------------------------

    def _get_bridge_url(self) -> str:
        return f"http://127.0.0.1:{self.bridge_port}"

    def _post_bridge(
        self,
        path: str,
        *,
        json_data: dict | None = None,
        timeout: float | None = None,
        raise_on_bridge_error: bool = True,
    ) -> Any:
        with self._bridge_control_lock:
            return self._post_bridge_once(
                path,
                json_data=json_data,
                timeout=timeout,
                raise_on_bridge_error=raise_on_bridge_error,
            )

    def _post_bridge_once(
        self,
        path: str,
        *,
        json_data: dict | None = None,
        timeout: float | None = None,
        raise_on_bridge_error: bool = True,
    ) -> Any:
        resp = self._bridge_session.post(
            f"{self._get_bridge_url()}{path}",
            json=json_data or {},
            timeout=timeout or self.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if raise_on_bridge_error and isinstance(data, dict) and "error" in data:
            raise RuntimeError(data["error"])
        return data

    def _get_bridge(self, path: str, *, params: dict | None = None, timeout: float = 10.0) -> Any:
        if path.startswith("/perception/"):
            return self._get_perception_bridge(path, params=params, timeout=timeout)
        return self._get_control_bridge(path, params=params, timeout=timeout)

    def _get_control_bridge(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: float = 10.0,
    ) -> Any:
        with self._bridge_control_lock:
            try:
                return self._get_bridge_once(path, params=params, timeout=timeout)
            except requests.RequestException as exc:
                if not self._should_reconnect(path, exc):
                    raise
                state = self._probe_bridge_state()
                if state == "alive":
                    return self._get_bridge_once(path, params=params, timeout=timeout)
                if state not in {"disconnected", "bridge_down"}:
                    raise
                self._reconnect_locked()
                return self._get_bridge_once(path, params=params, timeout=timeout)

    def _get_perception_bridge(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: float = 10.0,
    ) -> Any:
        with self._bridge_perception_lock:
            try:
                return self._get_bridge_once(
                    path,
                    params=params,
                    timeout=timeout,
                    session=self._perception_bridge_session,
                )
            except requests.RequestException as exc:
                if not self._should_reconnect(path, exc):
                    raise

                # A slow perception response is not evidence that the bot
                # disconnected. Check bridge state before attempting /start.
                state = self._probe_bridge_state()
                if state in {"disconnected", "bridge_down"}:
                    with self._bridge_control_lock:
                        self._reconnect_locked()
                return self._get_bridge_once(
                    path,
                    params=params,
                    timeout=timeout,
                    session=self._perception_bridge_session,
                )

    def _get_bridge_once(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> Any:
        resp = (session or self._bridge_session).get(
            f"{self._get_bridge_url()}{path}",
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _probe_bridge_state(self, timeout: float = 3.0) -> str:
        """Return bridge bot state without triggering another reconnect."""
        with self._bridge_control_lock:
            try:
                data = self._get_bridge_once("/status", timeout=timeout)
            except requests.RequestException:
                return "bridge_down" if not self.is_bridge_running() else "unknown"
            if self._status_has_live_bot(data):
                return "alive"
            if self._status_confirms_disconnected(data):
                return "disconnected"
            return "unknown"

    @staticmethod
    def _status_has_live_bot(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        if data.get("spawned") is True:
            return True
        if data.get("connectionState") in {"connected", "spawned"}:
            return True
        # Kept for compatibility with simple bridge test doubles.
        return data.get("status") in {"running", "spawned", "already_running"}

    @staticmethod
    def _status_confirms_disconnected(data: Any) -> bool:
        if not isinstance(data, dict) or data.get("spawned") is not False:
            return False
        return data.get("connectionState") in {"disconnected", "idle", "stopped"}

    def _should_reconnect(self, path: str, error: requests.RequestException) -> bool:
        if self._closing or not self._connect_payload or self.live is False:
            return False
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return True
        if not isinstance(error, requests.HTTPError) or error.response is None:
            return False
        if error.response.status_code not in {400, 502, 503, 504}:
            return False
        try:
            payload = error.response.json()
        except ValueError:
            payload = {}
        message = str(payload.get("error", "")) if isinstance(payload, dict) else ""
        return (
            error.response.status_code in {502, 503, 504}
            or message in {"Bot not started", "Bot not started or not spawned"}
        )

    def _reconnect_locked(self) -> None:
        if self._closing or not self._connect_payload or self.live is False:
            raise RuntimeError("bridge reconnect is not available")
        for attempt, delay in enumerate((0.0, 0.5, 1.0), start=1):
            if delay:
                time.sleep(delay)
            try:
                if self.is_bridge_running():
                    state = self._probe_bridge_state()
                    if state == "alive":
                        self._connected = True
                        return
                    if state != "disconnected":
                        raise RuntimeError(
                            f"bridge bot state is not confirmed disconnected: {state}"
                        )
                else:
                    self.start_bridge()
                data = self._post_bridge_once(
                    "/start",
                    json_data=self._connect_payload,
                    timeout=self.request_timeout,
                )
                if data.get("status") in {"spawned", "already_running"}:
                    self._connected = True
                    print(
                        f"[demo] {self.username} bridge reconnected "
                        f"(attempt={attempt})"
                    )
                    return
                raise RuntimeError(f"bot reconnect failed: {data}")
            except (requests.RequestException, RuntimeError) as exc:
                if attempt == 3:
                    raise RuntimeError(
                        f"{self.username} bridge reconnect failed after 3 attempts: {exc}"
                    ) from exc

    # ------------------------------------------------------------------
    # 进程生命周期
    # ------------------------------------------------------------------

    def start_bridge(self, ready_timeout: float = 30.0) -> None:
        """启动 bridge_skill.js 子进程并等待 HTTP 服务就绪。"""
        if self._monitor and self._monitor.is_running:
            return
        self._monitor = ManagedProcess(
            name=f"skill-bridge-{self.username}-{self.bridge_port}",
            cmd=["node", str(self.bridge_js)],
            cwd=self.bridge_js.parent,
            env={"BRIDGE_PORT": str(self.bridge_port), **self.bridge_env},
            ready_pattern=r"Bridge server started on port",
            fail_patterns=(r"Error:", r"error:"),
            log_dir=self.log_dir,
        )
        self._monitor.start()
        self._monitor.wait_ready(ready_timeout)

    def connect(self) -> dict[str, Any]:
        """启动 bridge 并让 bot 登录 Minecraft 服务器。"""
        self.start_bridge()
        self._connect_payload = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "initialYaw": 0.0,
            "initialPitch": 0.0,
            "auth": "offline",
            "viewDistance": "far",
            "mainHand": "right",
            "skinParts": {
                "showCape": True,
                "showJacket": True,
                "showLeftSleeve": True,
                "showRightSleeve": True,
                "showLeftPants": True,
                "showRightPants": True,
                "showHat": True,
            },
        }
        data = self._post_bridge(
            "/start",
            json_data=self._connect_payload,
            timeout=self.request_timeout,
        )
        if data.get("status") != "spawned":
            raise RuntimeError(f"bot 登录失败: {data}")
        self._connected = True
        return data

    def disconnect(self) -> None:
        """断开 bot 并停止 bridge 进程（不调用也不依赖外部进程）。"""
        with self._bridge_control_lock:
            self._closing = True
            try:
                self._post_bridge_once("/stop", timeout=5)
            except Exception:  # noqa: BLE001
                pass
            if self._monitor:
                self._monitor.stop()
                self._monitor = None
            self._connected = False
            self._closing = False

    def is_bridge_running(self) -> bool:
        """bridge 子进程是否仍在运行。"""
        return bool(self._monitor and self._monitor.is_running)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        return self._get_bridge("/status")

    def get_raw_perception(
        self,
        *,
        mode: str = "global",
        sub_mode: str = "radius",
        radius: float | None = None,
        block_radius: float | None = None,
        fov_degrees: float = 90.0,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        params = {
            "mode": mode,
            "subMode": sub_mode,
            "fovDegrees": fov_degrees,
        }
        if radius is not None:
            params["radius"] = radius
        if block_radius is not None:
            params["blockRadius"] = block_radius
        return filter_observer_players(
            self._get_bridge("/perception/raw", params=params, timeout=timeout)
        )

    def list_skills(self) -> dict[str, Any]:
        """返回本地 manifest 与 bridge 运行时能力（同 BaseActionPlanner.list_skills）。"""
        return self.action_planner.list_skills()

    def __repr__(self) -> str:
        return (
            f"SkillAgent(username={self.username!r}, bridge_port={self.bridge_port}, "
            f"connected={self._connected})"
        )
