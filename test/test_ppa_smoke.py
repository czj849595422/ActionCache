"""Offline unit tests for the PPA live smoke fixture (P0-6)."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.Parallelized.smoke import (  # noqa: E402
    ScriptedLLMClient,
    _decision_text,
    build_smoke_spec,
    default_sequences,
)
from src.tasks import BaseTaskManager, TaskSpec, TaskStatus  # noqa: E402


class FakeContext:
    def __init__(self, elapsed: float) -> None:
        self._elapsed = elapsed

    def elapsed_seconds(self) -> float:
        return self._elapsed


class ScriptedLLMClientTests(unittest.TestCase):
    def test_consumes_sequence_and_echoes_request_id(self) -> None:
        text = _decision_text("chat(message='x')")
        client = ScriptedLLMClient({"Alice": [text]})
        request = SimpleNamespace(metadata={"agent_id": "Alice"}, request_id="req-1")

        response = client.complete(request)

        self.assertEqual(response.content, text)
        self.assertEqual(response.request_id, "req-1")
        self.assertEqual(client.requests, [("Alice", request)])

        fallback = client.complete(request)
        self.assertIn("wait(seconds=1)", fallback.content)

    def test_unknown_agent_uses_default(self) -> None:
        client = ScriptedLLMClient()
        response = client.complete(
            SimpleNamespace(metadata={"agent_id": "nobody"}, request_id="r")
        )
        self.assertIn("wait(seconds=1)", response.content)

    def test_default_sequences_cover_interrupt(self) -> None:
        sequences = default_sequences()
        self.assertGreaterEqual(len(sequences["Alice"]), 4)
        self.assertIn('"interrupt": true', sequences["Alice"][3])
        self.assertIn("wait(seconds=30)", sequences["Alice"][0])


class SmokeSpecTests(unittest.TestCase):
    def test_build_smoke_spec(self) -> None:
        spec = build_smoke_spec(agents=2, duration_seconds=8, timeout_seconds=45)
        self.assertEqual(spec.category, "smoke")
        self.assertEqual(spec.agents["count"], 2)
        self.assertEqual(spec.agents["names"], ["Alice", "Bob"])
        self.assertEqual(spec.environment["world"], "base_env")
        self.assertEqual(
            spec.completion["success"]["type"],
            "survive_until",
        )

    def test_invalid_smoke_spec_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_smoke_spec(agents=0)
        with self.assertRaises(ValueError):
            build_smoke_spec(duration_seconds=50, timeout_seconds=45)


class SmokeManagerCompletionTests(unittest.TestCase):
    def test_survive_until_and_timeout(self) -> None:
        spec = build_smoke_spec(duration_seconds=8, timeout_seconds=45)
        manager = BaseTaskManager()
        manager.configure(spec)

        self.assertEqual(
            manager.completion_check(FakeContext(4)).status,
            TaskStatus.RUNNING,
        )
        self.assertEqual(
            manager.completion_check(FakeContext(9)).status,
            TaskStatus.SUCCESS,
        )

        timeout_spec = TaskSpec.from_dict(
            {
                "task_id": "ppa.smoke.timeout",
                "category": "smoke",
                "name": "T",
                "agents": {"count": 1, "names": ["Alice"]},
                "environment": {"world": "base_env"},
                "completion": {
                    "timeout_seconds": 45,
                    "success": {
                        "type": "survive_until",
                        "duration_seconds": 100,
                    },
                },
            }
        )
        timeout_manager = BaseTaskManager()
        timeout_manager.configure(timeout_spec)
        self.assertEqual(
            timeout_manager.completion_check(FakeContext(50)).status,
            TaskStatus.TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
