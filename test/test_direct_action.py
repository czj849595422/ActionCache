"""DirectAction 无推理直出动作 baseline 测试。"""

from __future__ import annotations

import csv
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.BasicAsync import (  # noqa: E402
    BasicActingWorker,
    BasicAsyncRunner,
    BasicPlanningWorker,
)
from src.baseline.COAP.codec import COAPDecisionCodec  # noqa: E402
from src.baseline.COAP.protocol import (  # noqa: E402
    COAPProtocolError,
    format_action_instructions,
    parse_action_response,
)
from src.baseline.DirectAction import (  # noqa: E402
    DirectActionConfig,
    DirectActionPromptBuilder,
    DirectActionRunner,
    DirectActionSequentialRunner,
)
from src.memory import TeamMemory  # noqa: E402
from src.recording.table import ActionExecutionTable  # noqa: E402
from src.runtime import SingleSlotOverwriteBuffer  # noqa: E402
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
            else "<action>wait(seconds=1)</action>"
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
    responses: list[str] | None = None,
    config: DirectActionConfig | None = None,
    execution_table=None,
    broadcast=None,
):
    agent = FakeAgent("Alice", mining_registry())
    config = config or DirectActionConfig()
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
        plan_source="direct",
    )
    planning = BasicPlanningWorker(
        agent=agent,
        teammates=(),
        llm_client=FakeLLM(responses),
        prompt_builder=DirectActionPromptBuilder(language=config.language),
        codec=codec,
        buffer=buffer,
        memory=memory,
        config=config,
        registry=agent.action_planner.registry,
        spec=fake_spec(),
        instructions=instructions,
        acting_worker=acting,
        operation="direct_action",
        path_label="direct",
    )
    return SimpleNamespace(
        agent=agent,
        memory=memory,
        buffer=buffer,
        acting=acting,
        planning=planning,
        codec=codec,
    )


class DirectActionProtocolTests(unittest.TestCase):
    def test_instructions_mention_action_and_chat_only(self) -> None:
        text = format_action_instructions("direct_action")
        self.assertIn("<action>", text)
        self.assertIn("<chat>", text)
        self.assertNotIn("<reason>", text)
        self.assertNotIn("<interrupt>", text)

    def test_parse_action_only(self) -> None:
        parsed = parse_action_response(
            "<action>wait(seconds=1)</action>",
            operation="direct_action",
        )
        self.assertEqual(parsed.action_text, "wait(seconds=1)")
        self.assertEqual(parsed.reasoning, "")
        self.assertFalse(parsed.interrupt)
        self.assertEqual(parsed.communication, "")

    def test_parse_action_with_chat(self) -> None:
        parsed = parse_action_response(
            "<action>wait(seconds=1)</action><chat>我去支援</chat>",
            operation="direct_action",
        )
        self.assertEqual(parsed.action_text, "wait(seconds=1)")
        self.assertEqual(parsed.communication, "我去支援")

    def test_extra_tags_are_ignored(self) -> None:
        parsed = parse_action_response(
            "<reason>无关推理</reason>"
            "<interrupt>true</interrupt>"
            "<action>goto(zombie_67)</action>"
            "<chat>go</chat>",
            operation="direct_action",
        )
        self.assertEqual(parsed.action_text, "goto(zombie_67)")
        self.assertEqual(parsed.reasoning, "")
        self.assertFalse(parsed.interrupt)
        self.assertEqual(parsed.communication, "go")

    def test_missing_action_rejected(self) -> None:
        with self.assertRaises(COAPProtocolError):
            parse_action_response("hello", operation="direct_action")

    def test_empty_action_rejected(self) -> None:
        with self.assertRaises(COAPProtocolError):
            parse_action_response(
                "<action>  </action>",
                operation="direct_action",
            )

    def test_multiline_chat_rejected(self) -> None:
        with self.assertRaises(COAPProtocolError):
            parse_action_response(
                "<action>wait(seconds=1)</action><chat>a\nb</chat>",
                operation="direct_action",
            )

    def test_standard_action_still_requires_reason(self) -> None:
        with self.assertRaises(COAPProtocolError):
            parse_action_response(
                "<action>wait(seconds=1)</action>",
                operation="standard_action",
            )


class DirectActionWorkerTests(unittest.TestCase):
    def test_planning_ignores_extra_tags_and_builds_proposal(self) -> None:
        env = build_workers(
            responses=[
                "<reason>ignored</reason>"
                "<interrupt>true</interrupt>"
                "<action>wait(seconds=1)</action>"
                "<chat>hi</chat>"
            ]
        )
        env.memory.update_observation(
            "Alice",
            "obs",
            metadata={"observed_at": 0.0, "format": "text"},
        )
        env.planning.iterate()
        proposal = env.buffer.peek()[1]
        self.assertEqual(env.planning.planned_count, 1)
        self.assertEqual(proposal.path, "direct")
        self.assertEqual(proposal.reasoning, "")
        self.assertEqual(proposal.communication, "hi")
        self.assertEqual(proposal.action_unit.action, "wait.wait")
        self.assertFalse(hasattr(env.planning.llm_client, "stream"))

    def test_execution_records_direct_path_and_plan_source(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            table = ActionExecutionTable(Path(tmp) / "actions.csv")
            env = build_workers(
                responses=["<action>wait(seconds=1)</action>"],
                execution_table=table,
            )
            env.memory.update_observation(
                "Alice",
                "obs",
                metadata={"observed_at": 0.0, "format": "text"},
            )
            env.planning.iterate()
            env.acting.iterate()
            env.acting.iterate()  # status completed → finish
            table.close()
            with (Path(tmp) / "actions.csv").open(
                encoding="utf-8-sig"
            ) as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][13], "direct")
            entries = env.memory.prompt_history("Alice")
            self.assertTrue(
                any(entry["metadata"].get("path") == "direct" for entry in entries)
            )
            direct_entry = next(
                entry for entry in entries if entry["metadata"].get("path") == "direct"
            )
            self.assertNotIn("standard_plan", direct_entry["content"])

    def test_communication_committed(self) -> None:
        broadcast_calls: list[str] = []
        env = build_workers(
            responses=["<action>wait(seconds=1)</action><chat>我去支援</chat>"],
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
        self.assertTrue(any("我去支援" in str(item["content"]) for item in history))
        self.assertTrue(any("我去支援" in text for text in broadcast_calls))


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


class DirectActionRunnerTests(unittest.TestCase):
    def test_uses_direct_action_owned_prompt_builder(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        runner = DirectActionRunner(
            session=FakeSession(fake_spec(), [agent]),
            llm_client=FakeLLM(),
            config=DirectActionConfig(),
        )

        self.assertIs(type(runner.prompt_builder), DirectActionPromptBuilder)
        self.assertEqual(
            runner.prompt_builder.template_dir,
            ROOT / "src" / "baseline" / "DirectAction" / "prompts",
        )

    def test_system_message_worker_is_disabled_by_default(self) -> None:
        class CountingManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "unused"

        manager = CountingManager()
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent], manager=manager)
        runner = DirectActionRunner(
            session=session,
            llm_client=FakeLLM(
                ["<reason>ignored</reason><action>wait(seconds=1)</action>"]
            ),
            config=DirectActionConfig(max_run_seconds=0.2),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
        )

        runner.run()

        self.assertEqual(manager.calls, 0)
        self.assertIsNone(runner._system_message_worker)

    def test_enabled_system_message_is_available_to_prompt_context(self) -> None:
        class OneShotManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "direct system note" if self.calls == 1 else None

        manager = OneShotManager()
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent], manager=manager)
        runner = DirectActionRunner(
            session=session,
            llm_client=FakeLLM(
                ["<reason>ignored</reason><action>wait(seconds=1)</action>"]
            ),
            config=DirectActionConfig(
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
        self.assertIn("say direct system note", session.commands)
        context = runner.memory.to_prompt_context("Alice", chat_window=5)
        self.assertIn(
            {"source": "system", "content": {"message": "direct system note"}},
            [{"source": item["source"], "content": item["content"]} for item in context["history"]],
        )

    def test_injects_direct_worker_options(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent])
        runner = DirectActionRunner(
            session=session,
            llm_client=FakeLLM(),
            config=DirectActionConfig(),
            observation_components_factory=lambda agent, rule: (
                FakePerception(),
                FakeVerbalizer(),
            ),
            planning_worker_kwargs={
                "operation": "standard_action",
                "path_label": "standard",
            },
            acting_worker_kwargs={"plan_source": "standard"},
        )
        self.assertEqual(
            runner._planning_worker_kwargs["operation"],
            "direct_action",
        )
        self.assertEqual(runner._planning_worker_kwargs["path_label"], "direct")
        self.assertEqual(runner._acting_worker_kwargs["plan_source"], "direct")

    def test_from_bundle_is_inherited(self) -> None:
        self.assertIs(
            DirectActionRunner.from_bundle.__func__,
            BasicAsyncRunner.from_bundle.__func__,
        )

    def test_end_to_end(self) -> None:
        agent = FakeAgent("Alice", mining_registry())
        session = FakeSession(fake_spec(), [agent])
        runner = DirectActionRunner(
            session=session,
            llm_client=FakeLLM(
                ["<reason>ignored</reason><action>wait(seconds=1)</action>"]
            ),
            config=DirectActionConfig(max_run_seconds=0.5),
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
        self.assertTrue(runner.llm_client.calls)
        request = runner.llm_client.calls[0]
        self.assertEqual(request.metadata["operation"], "direct_action")
        self.assertEqual(request.source.operation, "direct_action")
        system_content = request.messages[0]["content"]
        self.assertNotIn("<reason>", system_content)
        self.assertIn("<action>", system_content)


class DirectActionSequentialRunnerTests(unittest.TestCase):
    def test_wraps_generic_sequential_runner(self) -> None:
        self.assertEqual(
            DirectActionSequentialRunner.__mro__[1].__name__,
            "SequentialBaselineRunner",
        )
        sequential = DirectActionSequentialRunner(
            specs=[SimpleNamespace(task_id="task.1")],
            llm_client=SimpleNamespace(),
            config=DirectActionConfig(),
            run_recorder=SimpleNamespace(),
            server_dir=".",
            bridge_port_start=3201,
            benchmark_root=".",
        )
        self.assertIs(sequential.runner_class, DirectActionRunner)


if __name__ == "__main__":
    unittest.main()
