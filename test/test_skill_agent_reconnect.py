#!/usr/bin/env python3
"""SkillAgent bridge reconnect tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.url = "http://127.0.0.1:3201/test"

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response._content = json.dumps(self._payload).encode()
            response.url = self.url
            raise requests.HTTPError(response=response)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, get_results=(), post_result=None):
        self.trust_env = True
        self.get_calls = 0
        self.post_calls: list[str] = []
        self.get_results = list(get_results)
        self.post_result = post_result or _FakeResponse(200, {"status": "spawned"})

    def get(self, url, **kwargs):
        self.get_calls += 1
        if not self.get_results:
            return _FakeResponse(200, {"status": "running"})
        result = self.get_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        return self.post_result


class SkillAgentReconnectTests(unittest.TestCase):
    def _agent_with_payload(self):
        agent = SkillAgent(username="Alice", bridge_port=3201)
        agent._connect_payload = {
            "host": "localhost",
            "port": 25565,
            "username": "Alice",
        }
        return agent

    def test_perception_reconnects_only_after_disconnected_status(self):
        agent = self._agent_with_payload()
        agent._bridge_session = _FakeSession([
            _FakeResponse(
                200,
                {
                    "running": True,
                    "spawned": False,
                    "connectionState": "disconnected",
                },
            ),
        ])
        agent._perception_bridge_session = _FakeSession([
            requests.HTTPError(
                response=self._http_error_response(503),
            ),
            _FakeResponse(200, {"status": "running"}),
        ])

        with patch.object(agent, "start_bridge") as start_bridge:
            result = agent._get_bridge("/perception/raw", timeout=1.0)

        self.assertEqual(result["status"], "running")
        self.assertEqual(agent._perception_bridge_session.get_calls, 2)
        self.assertEqual(agent._bridge_session.get_calls, 1)
        self.assertEqual(len(agent._bridge_session.post_calls), 1)
        start_bridge.assert_called_once_with()
        self.assertTrue(agent._connected)

    def test_perception_failure_with_live_bot_does_not_post_start(self):
        agent = self._agent_with_payload()
        agent._bridge_session = _FakeSession([
            _FakeResponse(
                200,
                {
                    "running": True,
                    "spawned": True,
                    "connectionState": "connected",
                },
            ),
        ])
        agent._perception_bridge_session = _FakeSession([
            requests.HTTPError(response=self._http_error_response(503)),
            _FakeResponse(200, {"status": "running"}),
        ])

        with patch.object(agent, "start_bridge") as start_bridge:
            result = agent._get_bridge("/perception/raw", timeout=1.0)

        self.assertEqual(result["status"], "running")
        self.assertEqual(agent._perception_bridge_session.get_calls, 2)
        self.assertEqual(agent._bridge_session.get_calls, 1)
        self.assertEqual(agent._bridge_session.post_calls, [])
        start_bridge.assert_not_called()

    def test_perception_failure_with_unknown_running_bridge_does_not_reconnect(self):
        agent = self._agent_with_payload()
        agent._monitor = SimpleNamespace(is_running=True)
        agent._bridge_session = _FakeSession([requests.Timeout("status timeout")])
        agent._perception_bridge_session = _FakeSession([
            requests.Timeout("perception timeout"),
            _FakeResponse(200, {"status": "running"}),
        ])

        with patch.object(agent, "start_bridge") as start_bridge:
            result = agent._get_bridge("/perception/raw", timeout=1.0)

        self.assertEqual(result["status"], "running")
        self.assertEqual(agent._perception_bridge_session.get_calls, 2)
        self.assertEqual(agent._bridge_session.post_calls, [])
        start_bridge.assert_not_called()

    def test_bridge_uses_independent_perception_session_and_lock(self):
        agent = SkillAgent(username="Alice", bridge_port=3201)

        self.assertIsNot(agent._bridge_session, agent._perception_bridge_session)
        self.assertIsNot(agent._bridge_control_lock, agent._bridge_perception_lock)
        self.assertIs(agent._bridge_io_lock, agent._bridge_control_lock)

    def test_control_request_runs_while_perception_request_is_waiting(self):
        agent = self._agent_with_payload()
        perception_started = threading.Event()
        release_perception = threading.Event()
        perception_session = _FakeSession([
            _FakeResponse(200, {"status": "perception"}),
        ])

        original_get = perception_session.get

        def blocking_get(url, **kwargs):
            perception_started.set()
            if not release_perception.wait(timeout=2.0):
                raise AssertionError("perception request was not released")
            return original_get(url, **kwargs)

        perception_session.get = blocking_get
        agent._perception_bridge_session = perception_session
        agent._bridge_session = _FakeSession([
            _FakeResponse(200, {"status": "running"}),
        ])
        perception_result = []
        perception_thread = threading.Thread(
            target=lambda: perception_result.append(
                agent._get_bridge("/perception/raw", timeout=1.0)
            ),
        )
        perception_thread.start()
        self.assertTrue(perception_started.wait(timeout=1.0))

        control_result = []
        control_finished = threading.Event()

        def run_control():
            control_result.append(agent._get_bridge("/status"))
            control_finished.set()

        control_thread = threading.Thread(
            target=run_control,
        )
        control_thread.start()
        self.assertTrue(control_finished.wait(timeout=0.5))
        control_thread.join(timeout=1.0)
        self.assertEqual(control_result, [{"status": "running"}])

        release_perception.set()
        perception_thread.join(timeout=1.0)
        self.assertFalse(perception_thread.is_alive())
        self.assertEqual(perception_result, [{"status": "perception"}])

    @staticmethod
    def _http_error_response(status):
        response = requests.Response()
        response.status_code = status
        response._content = b'{"error":"Bot not started or not spawned"}'
        response.url = "http://127.0.0.1:3201/perception/raw"
        return response


if __name__ == "__main__":
    unittest.main()
