#!/usr/bin/env python3
"""Bridge request routing tests."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import BaseActionPlanner  # noqa: E402
from src.perception import BasePerception  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse({"status": "running"})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({"status": "completed"})


class BridgeAgent:
    username = "Alice"

    def __init__(self):
        self.calls = []

    def _get_bridge_url(self):
        return "http://127.0.0.1:3201"

    def _get_bridge(self, path, *, params=None, timeout=10.0):
        self.calls.append(("GET", path, params, timeout))
        return {"status": "running"}


class BridgeRequestRoutingTests(unittest.TestCase):
    def test_perception_reuses_agent_bridge_get(self):
        agent = BridgeAgent()
        perception = BasePerception(agent=agent, radius=20)

        result = perception.collect_raw()

        self.assertEqual(result["status"], "running")
        self.assertEqual(agent.calls[0][0:2], ("GET", "/perception/raw"))
        self.assertEqual(
            agent.calls[0][2],
            {"mode": "local", "subMode": "radius", "fovDegrees": 90.0, "radius": 20},
        )
        self.assertEqual(agent.calls[0][3], 10.0)

    def test_perception_retries_bridge_unavailable_response(self):
        agent = BridgeAgent()
        response = requests.Response()
        response.status_code = 503
        response._content = b'{"error":"Bot not started or not spawned"}'
        response.url = "http://127.0.0.1:3201/perception/raw"
        error = requests.HTTPError(response=response)
        calls = 0

        def get_bridge(path, *, params=None, timeout=10.0):
            nonlocal calls
            calls += 1
            agent.calls.append(("GET", path, params, timeout))
            if calls < 3:
                raise error
            return {"status": "running"}

        agent._get_bridge = get_bridge
        perception = BasePerception(agent=agent, radius=20)

        with patch("src.perception.base_perception.time.sleep") as sleep:
            result = perception.collect_raw()

        self.assertEqual(result["status"], "running")
        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_perception_retries_legacy_bridge_bad_request(self):
        agent = BridgeAgent()
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":"Bot not started or not spawned"}'
        response.url = "http://127.0.0.1:3201/perception/raw"
        error = requests.HTTPError(response=response)
        calls = 0

        def get_bridge(path, *, params=None, timeout=10.0):
            nonlocal calls
            calls += 1
            agent.calls.append(("GET", path, params, timeout))
            if calls < 3:
                raise error
            return {"status": "running"}

        agent._get_bridge = get_bridge
        perception = BasePerception(agent=agent, radius=20)

        with patch("src.perception.base_perception.time.sleep") as sleep:
            result = perception.collect_raw()

        self.assertEqual(result["status"], "running")
        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_planner_reuses_agent_bridge_get_for_status(self):
        agent = BridgeAgent()
        planner = BaseActionPlanner(agent)
        planner.active_actions["act-1"] = {"action": "wait.wait"}

        result = planner.status("act-1")

        self.assertEqual(result.status, "running")
        self.assertEqual(agent.calls, [
            ("GET", "/skill/status", {"actionId": "act-1"}, 5.0),
        ])

    def test_planner_fallback_requests_ignore_environment_proxy(self):
        session = FakeSession()
        agent = BridgeAgent()
        planner = BaseActionPlanner(agent)
        agent._get_bridge = None

        with patch(
            "src.agents.action.planner.requests.Session",
            return_value=session,
        ):
            planner._get("/skill/status")
            planner._post("/skill/execute", {})

        self.assertFalse(session.trust_env)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
