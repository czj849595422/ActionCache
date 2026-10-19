#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯任务环境 Runner 的生命周期与 debug 保留模式测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    TaskEnvironmentSession,
    TaskEnvironmentRunner,
    TaskOutcome,
    TaskSpec,
    TaskStatus,
)


def make_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "environment.runner.test",
            "category": "test",
            "name": "Environment Runner Test",
            "agents": {"count": 2, "names": ["Alice", "Bob"]},
            "completion": {
                "timeout_seconds": 60,
                "success": {"type": "survive_until", "duration_seconds": 10},
            },
        }
    )


class FakeServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = False
        self.stop_count = 0

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.events.append("server.start")
        self.running = True

    def stop(self) -> None:
        self.events.append("server.stop")
        self.running = False
        self.stop_count += 1


class FakeAgent:
    def __init__(self, username: str, events: list[str]) -> None:
        self.username = username
        self.events = events
        self.connected = False
        self.disconnect_count = 0

    def connect(self) -> None:
        self.events.append(f"{self.username}.connect")
        self.connected = True

    def disconnect(self) -> None:
        self.events.append(f"{self.username}.disconnect")
        self.connected = False
        self.disconnect_count += 1

    def get_raw_perception(self) -> dict[str, object]:
        return {"self": {"health": 20.0, "inventory": []}}


class FakeWorkspace:
    source_world = "fake/base_env"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared = False

    def prepare(self) -> None:
        self.events.append("workspace.prepare")
        self.prepared = True

    def cleanup(self) -> None:
        self.events.append("workspace.cleanup")
        self.prepared = False


class FakeManager:
    def __init__(
        self,
        events: list[str],
        outcomes: list[TaskStatus],
        fail_at: str | None = None,
    ) -> None:
        self.events = events
        self.outcomes = list(outcomes)
        self.fail_at = fail_at
        self.teardown_count = 0
        self.check_count = 0

    def configure(self, spec: TaskSpec) -> None:
        self.events.append("manager.configure")

    def prepare(self, server, agents, memory) -> None:
        self.events.append("manager.prepare")
        if self.fail_at == "prepare":
            raise RuntimeError("prepare failed")

    def spawn_initial_entities(self, server) -> None:
        self.events.append("manager.spawn_initial")

    def start(self) -> None:
        self.events.append("manager.start")

    def completion_check(self, context) -> TaskOutcome:
        self.events.append("manager.completion_check")
        self.check_count += 1
        if self.fail_at == "completion_check":
            raise RuntimeError("completion failed")
        status = self.outcomes.pop(0)
        return TaskOutcome(status, reason=status.value)

    def metrics(self, context) -> dict[str, object]:
        self.events.append("manager.metrics")
        return {"status": "sample"}

    def teardown(self) -> None:
        self.events.append("manager.teardown")
        self.teardown_count += 1


def make_runner(
    *,
    statuses: list[TaskStatus],
    debug: bool = False,
    keep_server: bool = False,
    debug_wait=None,
    fail_at: str | None = None,
    world_workspace=None,
):
    events: list[str] = []
    server = FakeServer(events)
    agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave", "Eve")[index], events) for index in range(2)]
    manager = FakeManager(events, statuses, fail_at=fail_at)
    outputs: list[str] = []
    outcomes: list[TaskOutcome] = []
    runner = TaskEnvironmentRunner(
        spec=make_spec(),
        manager=manager,
        server=server,
        agents=agents,
        world_workspace=world_workspace,
        poll_interval=0.001,
        debug=debug,
        keep_server=keep_server,
        prepare_seconds=0,
        output=outputs.append,
        outcome_reporter=outcomes.append,
        sleep=lambda _: None,
        debug_wait=debug_wait,
    )
    return runner, server, agents, manager, events, outputs, outcomes


class TaskEnvironmentRunnerTests(unittest.TestCase):
    def test_unified_prepare_window_before_initial_entities(self) -> None:
        events: list[str] = []
        server = FakeServer(events)
        agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave", "Eve")[index], events) for index in range(2)]
        manager = FakeManager(events, [TaskStatus.SUCCESS])
        outputs: list[str] = []
        session = TaskEnvironmentSession(
            spec=make_spec(),
            manager=manager,
            server=server,
            agents=agents,
            prepare_seconds=0.05,
            output=outputs.append,
        )
        session.start()

        self.assertIn("manager.prepare", events)
        self.assertIn("manager.spawn_initial", events)
        self.assertIn("manager.start", events)
        self.assertLess(
            events.index("manager.prepare"),
            events.index("manager.spawn_initial"),
        )
        self.assertLess(
            events.index("manager.spawn_initial"),
            events.index("manager.start"),
        )
        self.assertTrue(any("统一准备期" in text for text in outputs))
        self.assertTrue(any("准备期结束" in text for text in outputs))

    def test_prepare_window_disabled_with_zero(self) -> None:
        events: list[str] = []
        server = FakeServer(events)
        agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave", "Eve")[index], events) for index in range(2)]
        manager = FakeManager(events, [TaskStatus.SUCCESS])
        outputs: list[str] = []
        session = TaskEnvironmentSession(
            spec=make_spec(),
            manager=manager,
            server=server,
            agents=agents,
            prepare_seconds=0,
            output=outputs.append,
        )
        session.start()
        self.assertFalse(any("统一准备期" in text for text in outputs))
        self.assertIn("manager.spawn_initial", events)

    def test_task_timing_excludes_prepare_window(self) -> None:
        clock = [100.0]

        def advance_prepare(seconds: float) -> None:
            clock[0] += seconds

        session = TaskEnvironmentSession(
            spec=make_spec(),
            manager=FakeManager([], [TaskStatus.SUCCESS]),
            server=FakeServer([]),
            agents=[FakeAgent("Alice", [])],
            prepare_seconds=60.0,
            monotonic=lambda: clock[0],
            output=lambda _: None,
        )

        with patch(
            "src.tasks.environment_runner.time.sleep",
            side_effect=advance_prepare,
        ):
            context = session.start()

        self.assertEqual(context.elapsed_seconds(), 0.0)
        clock[0] += 5.0
        self.assertEqual(context.elapsed_seconds(), 5.0)

    def test_deferred_task_timing_starts_after_coap_preparation(self) -> None:
        clock = [100.0]
        session = TaskEnvironmentSession(
            spec=make_spec(),
            manager=FakeManager([], [TaskStatus.SUCCESS]),
            server=FakeServer([]),
            agents=[FakeAgent("Alice", [])],
            prepare_seconds=0,
            defer_task_timing=True,
            monotonic=lambda: clock[0],
            output=lambda _: None,
        )

        context = session.start()
        clock[0] = 105.0
        self.assertEqual(context.elapsed_seconds(), 0.0)

        session.start_task_timing()
        clock[0] = 107.5
        self.assertEqual(context.elapsed_seconds(), 2.5)

    def test_disposable_world_wraps_server_lifecycle(self) -> None:
        events: list[str] = []
        workspace = FakeWorkspace(events)
        runner, server, _, _, runner_events, _, _ = make_runner(
            statuses=[TaskStatus.SUCCESS],
            world_workspace=workspace,
        )
        workspace.events = runner_events

        result = runner.run()

        self.assertEqual(result.exit_code, 0)
        self.assertFalse(workspace.prepared)
        self.assertLess(
            runner_events.index("workspace.prepare"),
            runner_events.index("server.start"),
        )
        self.assertLess(
            runner_events.index("server.stop"),
            runner_events.index("workspace.cleanup"),
        )
        self.assertFalse(server.running)

    def test_normal_terminal_outcome_cleans_up_immediately(self) -> None:
        runner, server, agents, manager, events, _, outcomes = make_runner(
            statuses=[TaskStatus.RUNNING, TaskStatus.SUCCESS]
        )
        result = runner.run()

        self.assertEqual(result.outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.interrupted)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(manager.teardown_count, 1)
        self.assertTrue(all(not agent.connected for agent in agents))
        self.assertFalse(server.running)
        self.assertLess(events.index("manager.teardown"), events.index("Bob.disconnect"))
        self.assertLess(events.index("Alice.disconnect"), events.index("server.stop"))

    def test_debug_holds_all_terminal_outcomes_until_ctrl_c(self) -> None:
        for status in (TaskStatus.SUCCESS, TaskStatus.FAILURE, TaskStatus.TIMEOUT):
            with self.subTest(status=status):
                state: dict[str, object] = {}

                def interrupt_after_assertions() -> None:
                    server = state["server"]
                    agents = state["agents"]
                    manager = state["manager"]
                    outcomes = state["outcomes"]
                    self.assertTrue(server.running)
                    self.assertTrue(all(agent.connected for agent in agents))
                    self.assertEqual(manager.teardown_count, 0)
                    self.assertEqual(len(outcomes), 1)
                    raise KeyboardInterrupt

                runner, server, agents, manager, _, outputs, outcomes = make_runner(
                    statuses=[status],
                    debug=True,
                    debug_wait=interrupt_after_assertions,
                )
                state.update(
                    server=server,
                    agents=agents,
                    manager=manager,
                    outcomes=outcomes,
                )
                result = runner.run()

                self.assertEqual(result.outcome.status, status)
                self.assertTrue(result.interrupted)
                self.assertEqual(result.exit_code, 130)
                self.assertEqual(manager.check_count, 1)
                self.assertEqual(manager.teardown_count, 1)
                self.assertTrue(all(not agent.connected for agent in agents))
                self.assertFalse(server.running)
                self.assertTrue(any("debug 模式" in line for line in outputs))

    def test_debug_holds_disposable_world_until_ctrl_c(self) -> None:
        state: dict[str, object] = {}

        def interrupt_after_assertions() -> None:
            self.assertTrue(state["server"].running)
            self.assertTrue(state["workspace"].prepared)
            raise KeyboardInterrupt

        workspace = FakeWorkspace([])
        runner, server, _, _, events, _, _ = make_runner(
            statuses=[TaskStatus.TIMEOUT],
            debug=True,
            debug_wait=interrupt_after_assertions,
            world_workspace=workspace,
        )
        workspace.events = events
        state.update(server=server, workspace=workspace)

        result = runner.run()

        self.assertTrue(result.interrupted)
        self.assertFalse(server.running)
        self.assertFalse(workspace.prepared)
        self.assertLess(events.index("server.stop"), events.index("workspace.cleanup"))

    def test_keep_server_applies_after_cleanup(self) -> None:
        runner, server, agents, manager, _, _, _ = make_runner(
            statuses=[TaskStatus.TIMEOUT],
            keep_server=True,
        )
        result = runner.run()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(manager.teardown_count, 1)
        self.assertTrue(all(not agent.connected for agent in agents))
        self.assertTrue(server.running)
        self.assertEqual(server.stop_count, 0)

    def test_debug_keep_server_applies_after_ctrl_c(self) -> None:
        def interrupt() -> None:
            raise KeyboardInterrupt

        runner, server, agents, manager, _, _, _ = make_runner(
            statuses=[TaskStatus.SUCCESS],
            debug=True,
            keep_server=True,
            debug_wait=interrupt,
        )
        result = runner.run()

        self.assertTrue(result.interrupted)
        self.assertEqual(manager.teardown_count, 1)
        self.assertTrue(all(not agent.connected for agent in agents))
        self.assertTrue(server.running)
        self.assertEqual(server.stop_count, 0)

    def test_keep_server_is_rejected_for_disposable_world(self) -> None:
        with self.assertRaisesRegex(ValueError, "disposable world"):
            make_runner(
                statuses=[TaskStatus.SUCCESS],
                keep_server=True,
                world_workspace=FakeWorkspace([]),
            )

    def test_prepare_exception_still_cleans_up(self) -> None:
        runner, server, agents, manager, _, _, _ = make_runner(
            statuses=[TaskStatus.SUCCESS],
            fail_at="prepare",
        )
        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            runner.run()
        self.assertEqual(manager.teardown_count, 1)
        self.assertTrue(all(not agent.connected for agent in agents))
        self.assertFalse(server.running)

    def test_completion_exception_still_cleans_up(self) -> None:
        runner, server, agents, manager, _, _, _ = make_runner(
            statuses=[TaskStatus.SUCCESS],
            fail_at="completion_check",
        )
        with self.assertRaisesRegex(RuntimeError, "completion failed"):
            runner.run()
        self.assertEqual(manager.teardown_count, 1)
        self.assertTrue(all(not agent.connected for agent in agents))
        self.assertFalse(server.running)

    def test_runner_cannot_be_reused(self) -> None:
        runner, _, _, _, _, _, _ = make_runner(statuses=[TaskStatus.SUCCESS])
        runner.run()
        with self.assertRaisesRegex(RuntimeError, "only run once"):
            runner.run()


if __name__ == "__main__":
    unittest.main()
