"""BasicAsync 最普通异步规划 baseline 测试。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.BasicAsync import (  # noqa: E402
    BasicActingWorker,
    BasicAsyncConfig,
    BasicAsyncRunner,
    BasicPlanningWorker,
)
from src.baseline.COAP.codec import COAPDecisionCodec  # noqa: E402
from src.baseline.BasicAsync.prompts import BasicAsyncPromptBuilder  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.recording.table import ActionExecutionTable  # noqa: E402
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer  # noqa: E402
from src.tasks import TaskOutcome, TaskStatus, task_action_space  # noqa: E402


class FakeLLM:
    """仅支持 complete（无 stream），验证非流式规划。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list = []

    def _next(self) -> str:
        text = (
            self.responses.pop(0)
            if self.responses
            else "<reason>fallback</reason><action>wait(seconds=1)</action>"
        )
        self.calls.append(None)
        return text

    def complete(self, request):
        text = self._next()
        self.calls[-1] = request
        return SimpleNamespace(
            content=text,
            request_id=f"req-{len(self.calls)}",
            model="fake",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self.executed: list = []

    def execute_unit(self, unit):
        self.executed.append(unit)
        return SimpleNamespace(
            ok=True,
            status="completed",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "completed", "description": "ok"},
            raw={"ok": True},
        )

    def start_unit(self, unit):
        return SimpleNamespace(
            ok=True,
            status="running",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "running"},
            raw={"ok": True},
        )

    def status(self, action_id: str):
        return SimpleNamespace(
            ok=True,
            status="completed",
            result={"ok": True, "status": "completed"},
            raw={"ok": True},
        )

    def stop(self, action_id: str, reason: str = "stop"):
        return SimpleNamespace(
            ok=True,
            status="interrupted",
            result={"ok": True, "status": "interrupted"},
            raw={"ok": True},
        )


class FailingStatusPlanner(FakePlanner):
    def status(self, action_id: str):
        return SimpleNamespace(
            ok=False,
            status="failed",
            error="failed after start",
            result={
                "ok": False,
                "status": "failed",
                "error": "failed after start",
            },
            raw={"ok": False},
        )


class FakeAgent:
    def __init__(self, username: str, registry: SkillRegistry) -> None:
        self.username = username
        self.action_planner = FakePlanner(registry)


def mining_registry() -> SkillRegistry:
    from src.tasks import create_default_registry

    spec = create_default_registry().get("mining")
    return SkillRegistry(allowed_actions=task_action_space(spec))


def fake_spec():
    return SimpleNamespace(
        task_id="task.1",
        description="测试任务",
        category="mining",
        load_description_text=lambda: "测试任务描述",
    )


def build_workers(
    *,
    agent: FakeAgent | None = None,
    responses: list[str] | None = None,
    config: BasicAsyncConfig | None = None,
    execution_table=None,
    broadcast=None,
    task_category: str | None = None,
    liveness_gate: EntityLivenessGate | None = None,
):
    agent = agent or FakeAgent("Alice", mining_registry())
    config = config or BasicAsyncConfig()
    memory = TeamMemory()
    buffer = SingleSlotOverwriteBuffer()
    instructions = SimpleInstructionRegistry()
    codec = COAPDecisionCodec(
        agent.action_planner.registry,
        instructions,
        enable_chat_actions=config.enable_chat_actions,
    )
    acting = BasicActingWorker(
        agent=agent,
        buffer=buffer,
        memory=memory,
        config=config,
        instructions=instructions,
        broadcast=broadcast,
        execution_table=execution_table,
        task_category=task_category,
        liveness_gate=liveness_gate,
    )
    planning = BasicPlanningWorker(
        agent=agent,
        teammates=(),
        llm_client=FakeLLM(responses),
        prompt_builder=BasicAsyncPromptBuilder(language=config.language),
        codec=codec,
        buffer=buffer,
        memory=memory,
        config=config,
        registry=agent.action_planner.registry,
        spec=fake_spec(),
        instructions=instructions,
        acting_worker=acting,
        liveness_gate=liveness_gate,
    )
    return SimpleNamespace(
        agent=agent,
        memory=memory,
        buffer=buffer,
        acting=acting,
        planning=planning,
        codec=codec,
    )


class BasicPlanningWorkerTests(unittest.TestCase):
    def test_plan_once_when_idle_and_buffer_empty(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action><chat>hi</chat>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        self.assertTrue(env.buffer.has_new(0))
        self.assertEqual(env.planning.planned_count, 1)
        self.assertFalse(hasattr(env.planning.llm_client, "stream"))

    def test_executable_plan_chat_enters_memory_before_action_finishes(self) -> None:
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action><chat>ready now</chat>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertEqual(
            [item["content"]["communication"] for item in active_chats if item["content"].get("communication")],
            ["ready now"],
        )

        env.acting.iterate()
        env.acting.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertEqual(
            [item["content"]["communication"] for item in active_chats if item["content"].get("communication")],
            ["ready now"],
        )

    def test_dead_agent_skips_planning_and_memory_write(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        gate = EntityLivenessGate([agent])
        gate.mark_live("Alice", False, reason="test")
        env = build_workers(
            agent=agent,
            responses=["<reason>r</reason><action>wait(seconds=1)</action>"],
            liveness_gate=gate,
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()

        self.assertEqual(env.planning.llm_client.calls, [])
        self.assertFalse(env.buffer.has_new(0))
        self.assertEqual(env.memory.prompt_history("Alice"), [])

    def test_skip_planning_while_acting_busy(self) -> None:
        env = build_workers(
            responses=["<reason>r</reason><action>wait(seconds=1)</action>"]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        env.acting.iterate()  # 派发 continuous wait（busy）
        before = env.planning.planned_count
        env.planning.iterate()  # busy 时不规划
        self.assertEqual(env.planning.planned_count, before)

    def test_plan_failure_records_and_retries_next_round(self) -> None:
        events = []
        env = build_workers(responses=["not valid", "<reason>r</reason><action>wait(seconds=1)</action>"])
        env.planning.event_sink = lambda *a, **k: events.append(a[0])
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()  # 失败
        self.assertIn("planning.failed", events)
        self.assertFalse(env.buffer.has_new(0))
        env.planning.iterate()  # 下一轮成功
        self.assertTrue(env.buffer.has_new(0))

    def test_goto_string_position_binds_entity_name(self) -> None:
        env = build_workers(
            responses=["<reason>r</reason><action>goto(zombie_67)</action>"]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        proposal = env.buffer.peek()[1]
        self.assertEqual(
            proposal.action_unit.params.get("entity_name"),
            "zombie_67",
        )

    def test_goto_three_positional_arguments(self) -> None:
        env = build_workers(
            responses=["<reason>r</reason><action>goto(1, 0, 2)</action>"]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        proposal = env.buffer.peek()[1]
        params = proposal.action_unit.params
        self.assertEqual(params.get("x"), 1)
        self.assertEqual(params.get("y"), 0)
        self.assertEqual(params.get("z"), 2)

    def test_goto_tuple_position_argument(self) -> None:
        env = build_workers(
            responses=["<reason>r</reason><action>goto((1.5, 0.0, 2.5))</action>"]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        proposal = env.buffer.peek()[1]
        params = proposal.action_unit.params
        self.assertEqual(params.get("x"), 1.5)
        self.assertEqual(params.get("z"), 2.5)


class BasicActingWorkerTests(unittest.TestCase):
    def test_clearance_fight_defaults_collect_none(self) -> None:
        from src.baseline.COAP.schemas import COAPProposal  # noqa: PLC0415
        from src.interface import ActionUnit  # noqa: PLC0415

        def make_proposal():
            return COAPProposal(
                action_unit=ActionUnit(
                    agent_id="Alice",
                    action="combat.fight",
                    params={"collect_drops": "optional"},
                    mode="continuous",
                ),
                input_params={},
            )

        env = build_workers(task_category="monster_clearance")
        env.buffer.put(make_proposal())
        env.acting.iterate()
        self.assertEqual(
            env.acting.active_proposal.action_unit.params["collect_drops"],
            "none",
        )

        plain = build_workers()
        plain.buffer.put(make_proposal())
        plain.acting.iterate()
        self.assertEqual(
            plain.acting.active_proposal.action_unit.params["collect_drops"],
            "optional",
        )

    def test_action_feedback_broadcast_and_communication(self) -> None:
        broadcast_calls: list[str] = []
        env = build_workers(
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action><chat>我去支援</chat>"
            ],
            broadcast=broadcast_calls.append,
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        env.acting.iterate()
        env.acting.iterate()  # status completed → finish
        self.assertEqual(env.acting.executed_count, 1)
        history = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertTrue(any("完成" in str(item["content"]) for item in history))
        self.assertTrue(any("我去支援" in str(item["content"]) for item in history))
        self.assertTrue(any("完成" in text for text in broadcast_calls))
        self.assertTrue(any("我去支援" in text for text in broadcast_calls))

    def test_failed_executed_action_commits_communication(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        agent.action_planner = FailingStatusPlanner(agent.action_planner.registry)
        env = build_workers(
            agent=agent,
            responses=[
                "<reason>r</reason><action>wait(seconds=1)</action><chat>执行失败，换方案</chat>"
            ],
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )

        env.planning.iterate()
        env.acting.iterate()
        env.acting.iterate()

        active_chats = env.memory.prompt_history("Alice", team_limit=10, system_limit=10)
        self.assertTrue(
            any("执行失败，换方案" in str(item["content"]) for item in active_chats)
        )

    def test_execution_table_row_standard(self) -> None:
        import csv as csv_module

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            table = ActionExecutionTable(Path(tmp) / "actions.csv")
            env = build_workers(
                responses=["<reason>r</reason><action>wait(seconds=1)</action>"],
                execution_table=table,
            )
            env.memory.update_observation(
                "Alice",
                "obs",
                metadata={"observed_at": 0.0, "format": "text"},
            )
            env.planning.iterate()
            env.acting.iterate()
            env.acting.iterate()
            table.close()
            rows = list(
                csv_module.reader(
                    (Path(tmp) / "actions.csv").open(encoding="utf-8-sig")
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][13], "standard")
            self.assertEqual(rows[1][1], "Alice")


class FakePerception:
    def perceive(self) -> dict:
        return {"self": {"health": 20}, "metadata": {"mode": "global"}}


class FakeVerbalizer:
    def render(self, observation) -> str:
        return "测试观测文本"


class FakeSession:
    def __init__(self, spec, agents, *, manager=None) -> None:
        self.spec = spec
        self.agents = agents
        self.memory = TeamMemory()
        self.commands: list[str] = []
        self.server = SimpleNamespace(send_command=self.commands.append)
        self.manager = manager
        self.started = False
        self.closed = False
        self.context = None

    def start(self):
        self.started = True
        self.context = SimpleNamespace(elapsed_seconds=lambda: 0.0)
        return self.context

    def poll(self):
        return SimpleNamespace(
            outcome=TaskOutcome(TaskStatus.RUNNING),
            metrics={},
        )

    def close(self):
        self.closed = True
        return []


class BasicAsyncRunnerTests(unittest.TestCase):
    def test_wall_time_stops_at_task_terminal_detection(self) -> None:
        class Clock:
            value = 100.0

            def __call__(self) -> float:
                return self.value

        class TerminalSession(FakeSession):
            def __init__(self, *args, clock: Clock, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.clock = clock

            def poll(self):
                self.clock.value = 120.0
                return SimpleNamespace(
                    outcome=TaskOutcome(TaskStatus.SUCCESS),
                    metrics={},
                )

            def close(self):
                self.clock.value = 1000.0
                return super().close()

        clock = Clock()
        agent = FakeAgent("Alice", mining_registry())
        session = TerminalSession(fake_spec(), [agent], clock=clock)
        runner = BasicAsyncRunner(
            session=session,
            llm_client=FakeLLM(),
            config=BasicAsyncConfig(max_run_seconds=10.0),
            wall_time=clock,
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )

        result = runner.run()

        self.assertEqual(result.started_at, 100.0)
        self.assertEqual(result.finished_at, 120.0)

    def test_uses_basic_async_owned_prompt_builder(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        runner = BasicAsyncRunner(
            session=FakeSession(fake_spec(), [agent]),
            llm_client=FakeLLM(),
            config=BasicAsyncConfig(),
        )

        self.assertIs(type(runner.prompt_builder), BasicAsyncPromptBuilder)
        self.assertEqual(
            runner.prompt_builder.template_dir,
            ROOT / "src" / "baseline" / "BasicAsync" / "prompts",
        )

    def test_config_validates_system_message_fields(self) -> None:
        for kwargs in (
            {"system_messages_enabled": "yes"},
            {"system_message_interval": 0},
            {"system_message_interval": -1},
            {"system_message_interval": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BasicAsyncConfig(**kwargs)

    def test_end_to_end(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent])
        runner = BasicAsyncRunner(
            session=session,
            llm_client=FakeLLM(
                ["<reason>r</reason><action>wait(seconds=1)</action>"]
            ),
            config=BasicAsyncConfig(max_run_seconds=0.5),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )
        result = runner.run()
        self.assertTrue(session.started)
        self.assertTrue(session.closed)
        self.assertEqual(result.stop_reason, "max_run_seconds")
        self.assertGreaterEqual(result.planned_count, 0)
        self.assertIsNone(runner._system_message_worker)

    def test_enabled_system_message_is_available_to_prompt_context(self) -> None:
        class OneShotManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "system note" if self.calls == 1 else None

        manager = OneShotManager()
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent], manager=manager)
        runner = BasicAsyncRunner(
            session=session,
            llm_client=FakeLLM(
                ["<reason>r</reason><action>wait(seconds=1)</action>"]
            ),
            config=BasicAsyncConfig(
                max_run_seconds=0.2,
                system_messages_enabled=True,
                system_message_interval=0.01,
            ),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )

        runner.run()

        self.assertGreaterEqual(manager.calls, 1)
        self.assertIn("say system note", session.commands)
        context = runner.memory.to_prompt_context("Alice", chat_window=5)
        self.assertIn(
            {"source": "system", "content": {"message": "system note"}},
            [{"source": item["source"], "content": item["content"]} for item in context["history"]],
        )

    def test_session_started_event_recorded(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent])
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            events: list[str] = []
            runner = BasicAsyncRunner(
                session=session,
                llm_client=FakeLLM(),
                config=BasicAsyncConfig(max_run_seconds=0.3),
                observation_components_factory=lambda agent, rule: (
                    FakePerception(),
                    FakeVerbalizer(),
                ),
                episode_recorder=SimpleNamespace(
                    path=tmp,
                    event=lambda event_type, **data: events.append(event_type),
                    finalize=lambda **kw: None,
                    status="started",
                ),
            )
            try:
                runner.run()
            finally:
                if runner._execution_table is not None:
                    runner._execution_table.close()
            self.assertIn("session.started", events)


if __name__ == "__main__":
    unittest.main()
