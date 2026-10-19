#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/runtime 多 agent 编排器测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import LoopWorker, TeamRunner  # noqa: E402


class FakeServer:
    def __init__(self, running: bool = False):
        self.running = running
        self.started = 0
        self.stopped = 0

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.running = True
        self.started += 1

    def stop(self) -> None:
        self.running = False
        self.stopped += 1


class FakeAgent:
    def __init__(self, name: str, fail_connect: bool = False):
        self.name = name
        self.fail_connect = fail_connect
        self.connected = False
        self.disconnect_count = 0

    @property
    def username(self) -> str:
        return self.name

    def connect(self) -> None:
        if self.fail_connect:
            raise RuntimeError(f"connect failed: {self.name}")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1


class _StoppingWorker(LoopWorker):
    """迭代 target 次后自行停止的 worker。"""

    def __init__(self, name: str, target: int):
        super().__init__(name=name, loop_interval=0.01)
        self.target = target

    def iterate(self) -> None:
        if self.iteration_count + 1 >= self.target:
            self.stop(reason="done")


def _worker_factory(agent: FakeAgent, index: int) -> list[LoopWorker]:
    return [_StoppingWorker(name=f"{agent.name}-loop", target=3)]


def _completion_check(agents, workers) -> bool:
    return all(
        loop.iteration_count >= 3
        for loops in workers
        for loop in loops
    )


class TeamRunnerTests(unittest.TestCase):
    def test_start_orders_server_then_agents_then_workers(self) -> None:
        server = FakeServer()
        agent = FakeAgent("g1")
        runner = TeamRunner(
            server=server,
            agents=[agent],
            worker_factory=_worker_factory,
        )
        runner.start()
        self.assertTrue(server.running)
        self.assertTrue(agent.connected)
        self.assertTrue(runner.status()["agents"][0]["workers"][0]["running"])
        result = runner.wait(timeout=5.0)
        self.assertFalse(result.timed_out)
        runner.stop()
        self.assertFalse(agent.connected)
        self.assertFalse(server.running)

    def test_run_stops_and_cleans_up(self) -> None:
        server = FakeServer()
        agent = FakeAgent("g1")
        runner = TeamRunner(
            server=server,
            agents=[agent],
            worker_factory=_worker_factory,
            completion_check=_completion_check,
            poll_interval=0.01,
        )
        result = runner.run(timeout=5.0)
        self.assertFalse(result.timed_out)
        self.assertFalse(agent.connected)
        self.assertFalse(server.running)
        self.assertEqual(result.stop_reasons.get("g1-loop"), "done")
        self.assertEqual(server.started, 1)
        self.assertEqual(server.stopped, 1)
        self.assertEqual(agent.disconnect_count, 1)

    def test_wait_uses_completion_check(self) -> None:
        agent = FakeAgent("g1")
        runner = TeamRunner(
            agents=[agent],
            worker_factory=_worker_factory,
            completion_check=_completion_check,
            poll_interval=0.01,
        )
        with self.assertRaises(RuntimeError):
            runner.wait()
        runner.start()
        result = runner.wait(timeout=5.0)
        self.assertFalse(result.timed_out)
        runner.stop()

    def test_wait_before_start_raises(self) -> None:
        runner = TeamRunner(
            agents=[FakeAgent("g1")],
            worker_factory=_worker_factory,
        )
        with self.assertRaises(RuntimeError):
            runner.wait()

    def test_start_twice_raises(self) -> None:
        runner = TeamRunner(
            agents=[FakeAgent("g1")],
            worker_factory=_worker_factory,
        )
        runner.start()
        with self.assertRaises(RuntimeError):
            runner.start()
        runner.stop()

    def test_connect_failure_cleans_up(self) -> None:
        server = FakeServer()
        agent_ok = FakeAgent("g1")
        agent_bad = FakeAgent("g2", fail_connect=True)
        runner = TeamRunner(
            server=server,
            agents=[agent_ok, agent_bad],
            worker_factory=_worker_factory,
        )
        with self.assertRaises(RuntimeError):
            runner.start()
        self.assertFalse(agent_ok.connected)
        self.assertFalse(server.running)

    def test_status_reports_workers(self) -> None:
        agent = FakeAgent("g1")
        runner = TeamRunner(
            agents=[agent],
            worker_factory=_worker_factory,
        )
        status = runner.status()
        self.assertFalse(status["started"])
        self.assertEqual(status["agents"][0]["name"], "g1")
        runner.start()
        status = runner.status()
        self.assertTrue(status["started"])
        self.assertEqual(
            status["agents"][0]["workers"][0]["name"],
            "g1-loop",
        )
        runner.stop()

    def test_worker_error_recorded_in_result(self) -> None:
        class _BoomWorker(LoopWorker):
            def iterate(self) -> None:
                raise RuntimeError("boom")

        def factory(agent: FakeAgent, index: int) -> list[LoopWorker]:
            return [_BoomWorker(name=f"{agent.name}-loop")]

        agent = FakeAgent("g1")
        runner = TeamRunner(
            agents=[agent],
            worker_factory=factory,
            poll_interval=0.01,
        )
        result = runner.run(timeout=5.0)
        self.assertTrue(
            any(name == "g1-loop" and "boom" in message for name, message in result.errors)
        )
        self.assertFalse(agent.connected)


if __name__ == "__main__":
    unittest.main()
