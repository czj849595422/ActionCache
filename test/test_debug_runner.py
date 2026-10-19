#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 Debug Controller、REPL 与任务 adapter 测试。"""

from __future__ import annotations

import argparse
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.debug.cli as debug_cli  # noqa: E402
from src.agents.action.schemas import ActionResult  # noqa: E402
from src.debug import (  # noqa: E402
    DebugController,
    DebugOperationError,
    DebugRecorder,
    DebugRepl,
    PromptSnapshot,
    TaskDebugAdapter,
)
from src.tasks import (  # noqa: E402
    TaskEnvironmentSession,
    TaskOutcome,
    TaskSpec,
    TaskSpecError,
    TaskStatus,
    create_default_registry,
    expand_task_args,
    skill_agent_options,
)


class FakeDebugAdapter:
    def __init__(self) -> None:
        self.action_calls: list[tuple] = []
        self.rcon_commands: list[str] = []
        self.observation_calls: list[tuple[str, dict]] = []

    @property
    def agent_names(self) -> list[str]:
        return ["Alice", "Bob"]

    def execute_action(self, agent, operation, **kwargs):
        if agent not in self.agent_names:
            raise DebugOperationError("unknown_agent", f"Unknown agent: {agent}")
        self.action_calls.append((agent, operation, kwargs))
        if kwargs.get("action") == "fail.action":
            return ActionResult.failed("fail.action", "planned failure")
        return ActionResult(
            action=kwargs.get("action") or kwargs.get("action_id") or operation,
            ok=True,
            action_id=kwargs.get("action_id") or "action-1",
            status="completed",
            result={"operation": operation},
        )

    def send_server_command(self, command):
        self.rcon_commands.append(command)
        return "ok"

    def observe_agent(self, agent, options):
        self.observation_calls.append((agent, dict(options)))
        if agent == "Bob":
            raise RuntimeError("perception unavailable")
        return {"self": {"username": agent}, "options": dict(options)}

    def status(self):
        return {"server": {"running": True}, "monitor": {"running": True}}

    def metrics(self):
        return {"score": 3}

    def outcome(self):
        return None

    def component_providers(self):
        return {
            "task": lambda params: {"task_id": "fake", "params": params},
            "server": lambda params: {"running": True},
        }


class FakePromptProvider:
    name = "runner"

    def __init__(self) -> None:
        self.build_count = 0

    def list_methods(self, agent=None):
        return ["plan"]

    def build_prompt(self, agent, method):
        self.build_count += 1
        return PromptSnapshot(
            agent=agent,
            provider=self.name,
            method=method,
            messages=[
                {"role": "system", "content": "real system"},
                {"role": "user", "content": agent},
            ],
            model_parameters={"temperature": 0.2},
            input_sources={"observation": "adapter"},
        )


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.recorder = DebugRecorder(
            self.temp.name,
            {"task_id": "fake"},
            session_id="controller",
            artifact_threshold=256,
        )
        self.adapter = FakeDebugAdapter()
        self.prompt_provider = FakePromptProvider()
        self.controller = DebugController(
            self.adapter,
            self.recorder,
            prompt_providers=[self.prompt_provider],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_action_lifecycle_and_structured_failure(self) -> None:
        for operation, kwargs in (
            ("list", {}),
            ("run", {"action": "chat.say", "params": {"message": "hi"}}),
            ("start", {"action": "wait.wait", "params": {"seconds": 1}}),
            ("update", {"action_id": "a1", "params": {"seconds": 2}}),
            ("stop", {"action_id": "a1", "reason": "manual"}),
            ("status", {"action_id": "a1"}),
        ):
            with self.subTest(operation=operation):
                result = self.controller.execute_action("Alice", operation, **kwargs)
                self.assertTrue(result.ok, result.to_dict())

        failed = self.controller.execute_action(
            "Alice", "run", action="fail.action"
        )
        self.assertFalse(failed.ok)
        self.assertEqual(failed.error["code"], "action_failed")
        unknown = self.controller.execute_action("nobody", "list")
        self.assertEqual(unknown.error["code"], "unknown_agent")

    def test_rcon_preserves_command_exactly(self) -> None:
        command = "execute   as @a run say hello world  "
        result = self.controller.send_server_command(command)
        self.assertTrue(result.ok)
        self.assertEqual(self.adapter.rcon_commands, [command])
        self.assertEqual(result.result["command"], command)

    def test_observe_all_keeps_partial_results_and_writes_artifact(self) -> None:
        result = self.controller.observe_all({"mode": "global", "radius": 8})
        self.assertTrue(result.ok)
        self.assertTrue(result.result["agents"]["Alice"]["ok"])
        self.assertFalse(result.result["agents"]["Bob"]["ok"])
        self.assertEqual(result.result["task_metrics"], {"score": 3})
        self.assertIsNotNone(result.artifact_path)
        self.assertTrue(Path(result.artifact_path).is_file())

    def test_prompt_uses_registered_provider_and_unavailable_is_explicit(self) -> None:
        listed = self.controller.list_prompt_methods("Alice")
        self.assertEqual(listed.result["methods"], ["runner.plan"])
        shown = self.controller.build_prompt("Alice", "runner.plan")
        self.assertTrue(shown.ok)
        self.assertEqual(shown.result.messages[0]["content"], "real system")
        self.assertEqual(shown.result.model_parameters, {"temperature": 0.2})
        self.assertEqual(self.prompt_provider.build_count, 1)
        missing = self.controller.build_prompt("Alice", "runner.missing")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error["code"], "prompt_method_unavailable")

    def test_component_registry_rejects_arbitrary_introspection(self) -> None:
        listed = self.controller.list_components()
        self.assertIn("prompt_providers", listed.result["components"])
        shown = self.controller.inspect_component("task", {"detail": True})
        self.assertEqual(shown.result["task_id"], "fake")
        denied = self.controller.inspect_component("__dict__")
        self.assertEqual(denied.error["code"], "component_unavailable")

    def test_repl_routes_json_with_spaces_and_keeps_running_after_errors(self) -> None:
        output = StringIO()
        repl = DebugRepl(self.controller, stdout=output, output_limit=100000)
        self.assertIsNone(
            repl.onecmd('observe all {"mode": "global", "radius": 5}')
        )
        self.assertIsNone(repl.onecmd("action run Alice chat.say {bad json}"))
        self.assertIsNone(
            repl.onecmd('action run Alice chat.say {"message": "hello world"}')
        )
        self.assertTrue(repl.onecmd("exit"))
        self.assertIn('"operation": "observe.all"', output.getvalue())
        self.assertIn('"code": "invalid_syntax"', output.getvalue())

    def test_repl_rcon_preserves_spaces_after_command_separator(self) -> None:
        repl = DebugRepl(
            self.controller,
            stdout=StringIO(),
            output_limit=100000,
        )
        repl.onecmd("rcon   execute   as @a run say hi  ")
        self.assertEqual(
            self.adapter.rcon_commands[-1],
            "  execute   as @a run say hi  ",
        )

    def test_shorthand_repl_routes_call_text(self) -> None:
        output = StringIO()
        repl = DebugRepl(self.controller, stdout=output, output_limit=100000)
        self.assertIsNone(repl.onecmd("action run Alice chat(message='hi')"))
        agent, operation, kwargs = self.adapter.action_calls[-1]
        self.assertEqual(
            (agent, operation, kwargs["action"], kwargs["params"]),
            ("Alice", "run", "chat(message='hi')", {}),
        )
        self.assertIsNone(
            repl.onecmd("action run Alice chat(message='hi') {\"message\": \"x\"}")
        )
        self.assertIn('"code": "invalid_syntax"', output.getvalue())


def make_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "debug.adapter.test",
            "category": "test",
            "name": "Debug Adapter Test",
            "agents": {"count": 1, "names": ["Alice"]},
            "completion": {
                "timeout_seconds": 10,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


class FakePlanner:
    def __init__(self) -> None:
        self.internal_actions = frozenset({"container.register_chests"})
        self.calls: list[tuple] = []

    def execute(self, action, params, *, mode=None):
        self.calls.append(("execute", action, params, mode))
        return ActionResult(action=action, ok=True, status="completed")

    def start(self, action, params, *, mode=None):
        self.calls.append(("start", action, params, mode))
        return ActionResult(action=action, ok=True, action_id="a1", status="running")

    def update(self, action_id, params):
        return ActionResult(action=action_id, ok=True, status="running")

    def stop(self, action_id, reason=""):
        return ActionResult(action=action_id, ok=True, status="stopped")

    def status(self, action_id=None):
        return {"action_id": action_id, "status": "running"}


class FakeSessionAgent:
    def __init__(self, events: list[str], *, disconnect_failure=False) -> None:
        self.username = "Alice"
        self.bridge_port = 3201
        self.events = events
        self.disconnect_failure = disconnect_failure
        self.action_planner = FakePlanner()
        self.connected = False

    def connect(self):
        self.events.append("agent.connect")
        self.connected = True

    def disconnect(self):
        self.events.append("agent.disconnect")
        self.connected = False
        if self.disconnect_failure:
            raise RuntimeError("disconnect failed")

    def get_raw_perception(self, **kwargs):
        return {"self": {"health": 20, "inventory": []}, "options": kwargs}

    def list_skills(self):
        return {"local": {"chat": {"commands": ["say"]}}}

    def get_status(self):
        return {"spawned": self.connected}

    def is_bridge_running(self):
        return self.connected


class FakeSessionServer:
    def __init__(self, events):
        self.events = events
        self.running = False
        self.commands = []
        self.pid = 123

    @property
    def is_running(self):
        return self.running

    def start(self):
        self.events.append("server.start")
        self.running = True

    def stop(self):
        self.events.append("server.stop")
        self.running = False

    def send_command(self, command):
        self.commands.append(command)
        return "ok"


class FakeSessionManager:
    def __init__(self, events, *, fail_at=None, teardown_failure=False):
        self.events = events
        self.fail_at = fail_at
        self.teardown_failure = teardown_failure
        self.check_count = 0
        self.teardown_count = 0
        self.terminal_event = threading.Event()

    def configure(self, spec):
        self.events.append("manager.configure")

    def prepare(self, server, agents, memory):
        self.events.append("manager.prepare")
        if self.fail_at == "prepare":
            raise RuntimeError("prepare failed")

    def spawn_initial_entities(self, server):
        self.events.append("manager.spawn_initial")

    def start(self):
        self.events.append("manager.start")

    def completion_check(self, context):
        self.events.append("manager.check")
        self.check_count += 1
        self.terminal_event.set()
        if self.fail_at == "monitor":
            raise RuntimeError("monitor failed")
        return TaskOutcome(TaskStatus.SUCCESS, reason="done")

    def metrics(self, context):
        return {"checks": self.check_count}

    def teardown(self):
        self.events.append("manager.teardown")
        self.teardown_count += 1
        if self.teardown_failure:
            raise RuntimeError("teardown failed")


class TaskAdapterTests(unittest.TestCase):
    def make_adapter(
        self,
        *,
        fail_at=None,
        teardown_failure=False,
        disconnect_failure=False,
        spec=None,
    ):
        events: list[str] = []
        server = FakeSessionServer(events)
        agent = FakeSessionAgent(events, disconnect_failure=disconnect_failure)
        manager = FakeSessionManager(
            events,
            fail_at=fail_at,
            teardown_failure=teardown_failure,
        )
        session = TaskEnvironmentSession(
            spec=spec or make_spec(),
            manager=manager,
            server=server,
            agents=[agent],
            prepare_seconds=0,
            output=lambda _: None,
        )
        adapter = TaskDebugAdapter(session, poll_interval=0.01)
        return adapter, session, manager, server, agent, events

    def test_terminal_retains_environment_until_close_and_close_is_idempotent(self):
        adapter, session, manager, server, agent, events = self.make_adapter()
        adapter.start()
        self.assertTrue(manager.terminal_event.wait(1))
        deadline = time.time() + 1
        while adapter.outcome() is None and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(adapter.outcome()["status"], "success")
        self.assertTrue(server.running)
        self.assertTrue(agent.connected)
        self.assertEqual(manager.teardown_count, 0)
        self.assertEqual(manager.check_count, 1)

        first = adapter.close()
        second = adapter.close()
        self.assertEqual(first, second)
        self.assertFalse(server.running)
        self.assertFalse(agent.connected)
        self.assertEqual(manager.teardown_count, 1)
        self.assertLess(events.index("manager.teardown"), events.index("agent.disconnect"))
        self.assertLess(events.index("agent.disconnect"), events.index("server.stop"))

    def test_monitor_failure_keeps_environment_for_inspection(self):
        adapter, _, manager, server, _, _ = self.make_adapter(fail_at="monitor")
        adapter.start()
        self.assertTrue(manager.terminal_event.wait(1))
        deadline = time.time() + 1
        while adapter.status()["monitor"]["error"] is None and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(
            adapter.status()["monitor"]["error"]["code"],
            "monitor_failed",
        )
        self.assertTrue(server.running)
        adapter.close()

    def test_startup_failure_cleans_partial_environment(self):
        adapter, _, manager, server, agent, _ = self.make_adapter(fail_at="prepare")
        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            adapter.start()
        self.assertEqual(manager.teardown_count, 1)
        self.assertFalse(server.running)
        self.assertFalse(agent.connected)
        adapter.close()

    def test_internal_action_is_never_exposed(self):
        adapter, _, _, _, agent, _ = self.make_adapter()
        with self.assertRaises(DebugOperationError) as raised:
            adapter.execute_action(
                "Alice",
                "run",
                action="container.register_chests",
                params={},
            )
        self.assertEqual(raised.exception.code, "internal_action_forbidden")
        self.assertFalse(agent.action_planner.calls)

    def test_shorthand_action_supports_two_ways(self):
        adapter, _, _, _, agent, _ = self.make_adapter()

        adapter.execute_action("Alice", "run", action="chat(message='hi')")
        self.assertEqual(
            agent.action_planner.calls[-1],
            ("execute", "chat.say", {"message": "hi"}, "oneshot"),
        )

        adapter.execute_action(
            "Alice",
            "run",
            action="take_from('personal_box','iron_ingot',3)",
        )
        self.assertEqual(
            agent.action_planner.calls[-1],
            (
                "execute",
                "trade.take_from_container",
                {
                    "container": "personal_box",
                    "items": [{"name": "iron_ingot", "count": 3}],
                },
                "oneshot",
            ),
        )

        adapter.execute_action("Alice", "start", action="wait.wait(seconds=5)")
        self.assertEqual(
            agent.action_planner.calls[-1],
            ("start", "wait.wait", {"seconds": 5}, "continuous"),
        )

        with self.assertRaises(DebugOperationError) as raised:
            adapter.execute_action("Alice", "run", action="container.register_chests()")
        self.assertEqual(raised.exception.code, "internal_action_forbidden")

        with self.assertRaises(DebugOperationError) as raised:
            adapter.execute_action("Alice", "run", action="chat(")
        self.assertEqual(raised.exception.code, "invalid_shorthand_action")

        # 简写动作支持位置参数：chat('hi') 等价于 chat(message='hi')。
        adapter.execute_action("Alice", "run", action="chat('hi')")
        self.assertEqual(
            agent.action_planner.calls[-1],
            ("execute", "chat.say", {"message": "hi"}, "oneshot"),
        )

    def test_observation_supports_raw_world_and_text_representations(self):
        adapter, _, _, _, _, _ = self.make_adapter()
        raw = adapter.observe_agent("Alice", {"representation": "raw"})
        self.assertIn("self", raw)
        world = adapter.observe_agent("Alice", {"representation": "world"})
        self.assertEqual(world["schema_version"], "world_perception.v1")
        self.assertIn("self_state", world["sections"])
        text = adapter.observe_agent(
            "Alice",
            {
                "representation": "text",
                "sections": ["self_state", "inventory"],
            },
        )
        self.assertIn("【自身】", text)
        self.assertIn("【背包】", text)

    def test_observation_supports_registered_task_representations(self):
        spec = TaskSpec.from_dict(
            {
                "task_id": "mining.debug.test",
                "category": "mining",
                "name": "Mining Debug Test",
                "agents": {"count": 1, "names": ["Alice"]},
                "completion": {
                    "timeout_seconds": 180,
                    "success": {
                        "type": "item_value",
                        "target_value": 80,
                        "item_values": {"iron_block": 3},
                    },
                },
            }
        )
        adapter, _, _, _, _, _ = self.make_adapter(spec=spec)
        adapter._last_metrics = {  # task overlay uses the monitor's latest snapshot
            "current_value": 3,
            "target_value": 80,
            "item_counts": {"iron_block": 1},
            "active_groups": 2,
            "command_errors": 0,
        }
        task = adapter.observe_agent("Alice", {"representation": "task"})
        self.assertEqual(task["schema_version"], "task_perception.v1")
        self.assertEqual(task["category"], "mining")
        self.assertNotIn("projectiles_explosives", task["sections"])
        text = adapter.observe_agent("Alice", {"representation": "task_text"})
        self.assertIn("【任务进度】", text)
        self.assertIn("时间：0/180；收集进度：3/80。", text)

        with self.assertRaises(DebugOperationError) as raised:
            adapter.observe_agent(
                "Alice",
                {"representation": "task", "sections": ["self_state"]},
            )
        self.assertEqual(raised.exception.code, "invalid_task_observation_options")

    def test_observation_rejects_invalid_world_options(self):
        adapter, _, _, _, _, _ = self.make_adapter()
        with self.assertRaises(DebugOperationError) as raised:
            adapter.observe_agent("Alice", {"representation": "server"})
        self.assertEqual(
            raised.exception.code, "invalid_observation_representation"
        )
        with self.assertRaises(DebugOperationError) as raised:
            adapter.observe_agent(
                "Alice", {"representation": "world", "sections": "players"}
            )
        self.assertEqual(raised.exception.code, "invalid_observation_sections")

    def test_cleanup_continues_after_multiple_failures(self):
        adapter, _, manager, server, _, events = self.make_adapter(
            teardown_failure=True,
            disconnect_failure=True,
        )
        adapter.start()
        manager.terminal_event.wait(1)
        errors = adapter.close()
        self.assertEqual({name for name, _ in errors}, {"task_manager", "Alice"})
        self.assertIn("server.stop", events)
        self.assertFalse(server.running)


class EnvironmentFactoryPermissionTests(unittest.TestCase):
    def test_trade_default_and_unsafe_permissions_hide_internal_actions(self):
        spec = create_default_registry().get("trade")
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            default = skill_agent_options(spec, root, root / "chests.json")
            self.assertIn("trade.inspect_shop", default["allowed_actions"])
            self.assertNotIn(
                "container.register_chests", default["allowed_actions"]
            )
            unsafe = skill_agent_options(
                spec,
                root,
                root / "chests.json",
                unsafe_actions=True,
            )
            self.assertIn("container.inspect_chest", unsafe["allowed_actions"])
            self.assertNotIn(
                "container.register_chests", unsafe["allowed_actions"]
            )
            self.assertEqual(
                unsafe["internal_actions"],
                frozenset({"container.register_chests"}),
            )


class DebugBatchRunTests(unittest.TestCase):
    def make_args(self, root: Path, **overrides) -> argparse.Namespace:
        data = {
            "debug_root": root,
            "task": "debug@10",
            "task_seed": None,
            "start_episode": 1,
            "episode_count": 2,
            "max_run_seconds": 30.0,
            "run_id": None,
            "unsafe_actions": False,
            "server_dir": ROOT / "src" / "minecraft",
            "bridge_port_start": 3201,
            "poll_interval": 0.1,
        }
        data.update(overrides)
        return argparse.Namespace(**data)

    def run_with_fake_smoke(self, args: argparse.Namespace):
        specs = expand_task_args(
            [args.task],
            seed=args.task_seed,
            start_episode=args.start_episode,
            episode_count=args.episode_count,
        )
        calls: list[tuple[int, int | None, Path]] = []

        def fake_smoke_episode(
            smoke_args,
            registry,
            spec,
            run_dir: Path,
            run_id: str,
            episode_number: int,
        ):
            calls.append(
                (
                    episode_number,
                    (spec.params or {}).get("generation_seed"),
                    run_dir,
                )
            )
            return {
                "episode_id": str(episode_number),
                "task_id": spec.task_id,
                "category": spec.category,
                "seed": (spec.params or {}).get("generation_seed"),
                "status": "completed",
                "elapsed_seconds": 0.01,
                "path": str(run_dir / str(episode_number)),
                "error": None,
            }

        original = debug_cli._run_smoke_episode
        try:
            debug_cli._run_smoke_episode = fake_smoke_episode
            code = debug_cli._run_batch_smoke(
                args,
                specs,
                create_default_registry(),
            )
        finally:
            debug_cli._run_smoke_episode = original
        return code, calls

    def test_batch_run_records_multiple_seeded_episodes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            args = self.make_args(Path(directory), task="debug@10", episode_count=2)
            code, calls = self.run_with_fake_smoke(args)

            self.assertEqual(code, 0)
            self.assertEqual(
                [(episode, seed) for episode, seed, _ in calls],
                [(1, 10), (2, 11)],
            )
            run_dirs = [path for path in Path(directory).iterdir() if path.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            manifest = json.loads(
                (run_dirs[0] / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["task_id"], "debug")
            self.assertEqual(
                [episode["seed"] for episode in manifest["episodes"]],
                [10, 11],
            )
            self.assertEqual(
                [episode["episode_id"] for episode in manifest["episodes"]],
                ["1", "2"],
            )

    def test_batch_resume_appends_next_episode_seed(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            first_args = self.make_args(root, task="debug@10", episode_count=2)
            self.run_with_fake_smoke(first_args)
            run_dir = next(path for path in root.iterdir() if path.is_dir())
            run_id = run_dir.name

            resume_args = self.make_args(
                root,
                task="debug@10",
                run_id=run_id,
                start_episode=3,
                episode_count=1,
            )
            code, calls = self.run_with_fake_smoke(resume_args)

            self.assertEqual(code, 0)
            self.assertEqual(
                [(episode, seed) for episode, seed, _ in calls],
                [(3, 12)],
            )
            manifest = json.loads(
                (run_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertIn("resumed_at", manifest)
            self.assertEqual(
                [episode["seed"] for episode in manifest["episodes"]],
                [10, 11, 12],
            )
            self.assertEqual(
                [episode["episode_id"] for episode in manifest["episodes"]],
                ["1", "2", "3"],
            )

    def test_batch_resume_rejects_episode_gap_and_unsafe_run_id(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            first_args = self.make_args(root, task="debug@10", episode_count=2)
            self.run_with_fake_smoke(first_args)
            run_id = next(path.name for path in root.iterdir() if path.is_dir())

            gap_args = self.make_args(
                root,
                task="debug@10",
                run_id=run_id,
                start_episode=4,
                episode_count=1,
            )
            specs = expand_task_args(
                [gap_args.task],
                start_episode=gap_args.start_episode,
                episode_count=gap_args.episode_count,
            )
            with self.assertRaisesRegex(TaskSpecError, "已有 2 个 episode"):
                debug_cli._run_batch_smoke(
                    gap_args,
                    specs,
                    create_default_registry(),
                )

            unsafe_args = self.make_args(
                root,
                task="debug@10",
                run_id="..\\escape",
                start_episode=3,
                episode_count=1,
            )
            with self.assertRaisesRegex(TaskSpecError, "--run-id"):
                debug_cli._open_or_create_debug_run(unsafe_args, specs)


if __name__ == "__main__":
    unittest.main()
