#!/usr/bin/env python3
"""Lifecycle and two-agent acceptance tests for PPARunner."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import (  # noqa: E402
    ActionResult,
    ActionStatus,
    SkillRegistry,
)
from src.baseline.Parallelized import (  # noqa: E402
    JsonDecisionCodec,
    PPAConfig,
    PPARunner,
)
from src.baseline.Parallelized.budget import RunBudget  # noqa: E402
from src.interface import ActionResultEnvelope  # noqa: E402
from src.llm import LLMClient, LLMConfig  # noqa: E402
from src.perception.rule.base.base_rule import RuleContext  # noqa: E402
from src.recording import RecordingConfig, RecordingManager  # noqa: E402
from src.tasks import (  # noqa: E402
    TaskEnvironmentPoll,
    TaskOutcome,
    TaskSpec,
    TaskStatus,
    create_default_registry,
)


def make_spec(agent_count: int = 2) -> TaskSpec:
    names = [f"a{index}" for index in range(agent_count)]
    return TaskSpec.from_dict(
        {
            "task_id": "ppa.runner.test",
            "category": "test",
            "name": "PPA Runner Test",
            "description": "exercise the runner",
            "agents": {"count": agent_count, "names": names},
            "completion": {
                "timeout_seconds": 10,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


class FakeContext:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started


class FakeSession:
    def __init__(
        self,
        agents,
        events: list[str],
        *,
        statuses=None,
        terminal_condition=None,
        poll_exception: BaseException | None = None,
    ) -> None:
        self.spec = make_spec(len(agents))
        self.agents = list(agents)
        self.events = events
        self.memory = None
        self.context = None
        self.statuses = list(statuses or [])
        self.terminal_condition = terminal_condition
        self.poll_exception = poll_exception
        self.poll_count = 0
        self.close_count = 0

    def start(self) -> FakeContext:
        self.events.append("session.start")
        self.context = FakeContext()
        return self.context

    def poll(self) -> TaskEnvironmentPoll:
        self.poll_count += 1
        self.events.append("session.poll")
        if self.poll_exception is not None:
            raise self.poll_exception
        if self.terminal_condition is not None:
            status = (
                TaskStatus.SUCCESS
                if self.terminal_condition()
                else TaskStatus.RUNNING
            )
            if self.poll_count > 300:
                status = TaskStatus.FAILURE
        elif self.statuses:
            status = self.statuses.pop(0)
        else:
            status = TaskStatus.RUNNING
        return TaskEnvironmentPoll(
            TaskOutcome(status, reason=status.value),
            {"poll_count": self.poll_count},
        )

    def close(self):
        self.close_count += 1
        self.events.append("session.close")
        return []


class FakePerception:
    def __init__(self, agent_id: str, events: list[str], *, fail: bool = False) -> None:
        self.agent_id = agent_id
        self.events = events
        self.fail = fail
        self.rule = None

    def perceive(self):
        self.events.append(f"{self.agent_id}.observe")
        if self.fail:
            raise RuntimeError("observation unavailable")
        return {"agent": self.agent_id, "health": 20}


class FakeVerbalizer:
    def render(self, observation) -> str:
        return f"{observation['agent']} health={observation['health']}"


class FakePlanner:
    def __init__(self, agent_id: str, events: list[str], *, keep_running: bool = False):
        self.agent_id = agent_id
        self.events = events
        self.keep_running = keep_running
        self.registry = SkillRegistry(allowed_actions={"wait.wait", "chat.say"})
        self.started = []
        self.executed = []
        self.stopped = []
        self._next_id = 0

    def start_unit(self, unit):
        self._next_id += 1
        action_id = f"{self.agent_id}-active-{self._next_id}"
        self.started.append(unit)
        self.events.append(f"{self.agent_id}.start:{unit.action}")
        return ActionResultEnvelope(
            action=unit.action,
            ok=True,
            agent_id=self.agent_id,
            action_id=action_id,
            status=ActionStatus.RUNNING,
        )

    def status(self, action_id: str):
        status = ActionStatus.RUNNING if self.keep_running else ActionStatus.COMPLETED
        return ActionResult(
            "wait.wait",
            True,
            action_id=action_id,
            status=status,
            result={"seconds": 0},
        )

    def stop(self, action_id: str, *, reason: str):
        self.stopped.append((action_id, reason))
        self.events.append(f"{self.agent_id}.stop:{reason}")
        return ActionResult(
            "wait.wait",
            True,
            action_id=action_id,
            status=ActionStatus.STOPPED,
        )

    def execute_unit(self, unit):
        self.executed.append(unit)
        self.events.append(f"{self.agent_id}.execute:{unit.params.get('message', '')}")
        return ActionResultEnvelope(
            action=unit.action,
            ok=True,
            agent_id=self.agent_id,
            status=ActionStatus.COMPLETED,
            result={"message": unit.params.get("message")},
        )


class FakeAgent:
    def __init__(self, agent_id: str, events: list[str], *, keep_running=False) -> None:
        self.username = agent_id
        self.action_planner = FakePlanner(
            agent_id,
            events,
            keep_running=keep_running,
        )


class FakeStoppedServer:
    @property
    def is_running(self) -> bool:
        return False


class FakeLLM:
    def __init__(
        self,
        responses=None,
        *,
        delay: float = 0.0,
        default_text: str | None = None,
    ) -> None:
        self.responses = {
            key: list(value) for key, value in (responses or {}).items()
        }
        self.delay = delay
        self.default_text = default_text or (
            '{"skill": "chat.say(message=\'idle\')", "interrupt": false, '
            '"reason": "idle"}'
        )
        self.calls = defaultdict(int)
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def complete(self, request):
        agent_id = request.metadata["agent_id"]
        with self.lock:
            self.requests.append(request)
            self.calls[agent_id] += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            queue = self.responses.get(agent_id)
            if queue:
                text = queue.pop(0)
            else:
                text = self.default_text
            return SimpleNamespace(content=text, request_id=request.request_id)
        finally:
            with self.lock:
                self.active -= 1


def config(**changes) -> PPAConfig:
    values = {
        "observation_interval": 0.005,
        "planning_interval": 0.001,
        "acting_poll_interval": 0.002,
        "task_poll_interval": 0.005,
        "initial_observation_timeout": 0.3,
        "worker_stop_timeout": 0.3,
        "max_consecutive_errors": 2,
    }
    values.update(changes)
    return PPAConfig(**values)


CONTINUOUS_TEXT = (
    '{"skill": "wait.wait(seconds=0)", "interrupt": false, '
    '"reason": "idle"}'
)


def observation_factory(events: list[str], failures=frozenset()):
    def create(agent, rule_factory):
        return (
            FakePerception(agent.username, events, fail=agent.username in failures),
            FakeVerbalizer(),
        )

    return create


class PPARunnerTests(unittest.TestCase):
    def test_real_llm_client_records_two_planning_workers_in_episode(self) -> None:
        class Message:
            content = (
                '{"skill": "chat.say(message=\'ready\')", "interrupt": false, '
                '"reason": "ready"}'
            )
            reasoning_content = ""

            def model_dump(self, **_):
                return {"content": self.content, "reasoning_content": ""}

        class Response:
            model = "fake-model"
            usage = None
            choices = [SimpleNamespace(message=Message(), finish_reason="stop")]

            def model_dump(self, **_):
                return {"model": self.model}

        class Completions:
            async def create(self, **_):
                await asyncio.sleep(0.005)
                return Response()

        with tempfile.TemporaryDirectory() as tmp:
            run = RecordingManager(
                RecordingConfig(root_dir=Path(tmp))
            ).start_run("parallelized")
            episode = run.start_episode(metadata={"task_id": "ppa.runner.test"})
            client = LLMClient(
                LLMConfig(
                    api_url="https://example.invalid",
                    api_key="test-key",
                    model="fake-model",
                    max_retries=0,
                ),
                client=SimpleNamespace(
                    chat=SimpleNamespace(completions=Completions())
                ),
            )
            events: list[str] = []
            agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
            runner = PPARunner(
                session=FakeSession(
                    agents,
                    events,
                    statuses=[TaskStatus.RUNNING] * 5 + [TaskStatus.SUCCESS],
                ),
                llm_client=client,
                config=config(),
                episode_recorder=episode,
                observation_components_factory=observation_factory(events),
            )

            result = runner.run()
            client.close()
            run.finalize()

            self.assertEqual(result.exit_code, 0)
            payloads = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (episode.path / "llm" / "calls").glob("*.json")
            ]
            self.assertGreaterEqual(len(payloads), 2)
            self.assertEqual(
                {payload["source"]["agent_id"] for payload in payloads},
                {"a0", "a1"},
            )
            self.assertTrue(
                all(
                    payload["source"]["component"] == "planning_worker"
                    and payload["source"]["task_id"] == "ppa.runner.test"
                    for payload in payloads
                )
            )

    def test_episode_recorder_captures_result_memory_events_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = RecordingManager(
                RecordingConfig(root_dir=Path(tmp))
            ).start_run("parallelized")
            episode = run.start_episode(metadata={"task_id": "ppa.runner.test"})
            events: list[str] = []
            agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
            llm = FakeLLM(delay=0.005)
            runner = PPARunner(
                session=FakeSession(
                    agents,
                    events,
                    statuses=[TaskStatus.RUNNING] * 3 + [TaskStatus.SUCCESS],
                ),
                llm_client=llm,
                config=config(),
                episode_recorder=episode,
                observation_components_factory=observation_factory(events),
            )

            result = runner.run()
            run.finalize()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(episode.status, "completed")
            self.assertTrue((episode.path / "result.json").is_file())
            self.assertTrue((episode.path / "memory.json").is_file())
            event_types = [
                json.loads(line)["type"]
                for line in (episode.path / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn("runner.started", event_types)
            self.assertIn("task.updated", event_types)
            self.assertIn("runner.closed", event_types)
            self.assertTrue(llm.requests)
            self.assertTrue(
                all(request.source.component == "planning_worker" for request in llm.requests)
            )
            self.assertEqual(
                {request.source.task_id for request in llm.requests},
                {"ppa.runner.test"},
            )

    def test_from_bundle_shares_memory_and_owns_only_runtime_cleanup(self) -> None:
        events: list[str] = []
        llm = FakeLLM()
        llm.close = lambda: events.append("llm.close")
        bundle = SimpleNamespace(
            spec=make_spec(1),
            manager=None,
            server=FakeStoppedServer(),
            agents=[FakeAgent("a0", events)],
            world_workspace=None,
            registry_file=ROOT / ".tmp" / "unused.json",
            close_runtime=lambda: events.append("runtime.close"),
        )

        runner = PPARunner.from_bundle(bundle, llm_client=llm, output=lambda _: None)
        runner.close()
        runner.close()

        self.assertIs(runner.session.memory, runner.memory)
        self.assertEqual(events.count("runtime.close"), 1)
        self.assertNotIn("llm.close", events)

    def test_two_agents_plan_concurrently_and_cleanup_after_success(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
        session = FakeSession(
            agents,
            events,
            statuses=[TaskStatus.RUNNING] * 8 + [TaskStatus.SUCCESS],
        )
        closed = []
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(delay=0.01),
            config=config(),
            runtime_closer=lambda: (events.append("runtime.close"), closed.append(True)),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(result.planned_count, 2)
        self.assertGreaterEqual(result.executed_count, 2)
        self.assertGreaterEqual(runner.llm_client.max_active, 2)
        self.assertIs(session.memory, runner.memory)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(len(closed), 1)
        self.assertTrue(runner.status()["closed"])
        self.assertTrue(
            all(
                not worker["running"]
                for agent in runner.status()["agents"].values()
                for worker in agent.values()
            )
        )

    def test_default_observation_components_use_task_observation_range(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events)
        session = FakeSession([agent], events)
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
        )

        perception, _ = runner._default_observation_components(
            agent,
            lambda: None,
        )
        self.assertEqual(perception.radius, 20.0)
        self.assertEqual(perception.mode, "local")

        session.spec.environment["observation_range"] = 25
        perception2, _ = runner._default_observation_components(
            agent,
            lambda: None,
        )
        self.assertEqual(perception2.radius, 25.0)

    def test_runner_codec_defaults_to_simple_instructions(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events)
        session = FakeSession([agent], events)
        simple_runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
        )
        self.assertIsInstance(simple_runner.codec, JsonDecisionCodec)

        legacy_runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(simple_instructions=False),
        )
        self.assertIsInstance(legacy_runner.codec, JsonDecisionCodec)

    def test_broadcast_results_switch_controls_result_channel(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
        session = FakeSession(
            agents,
            events,
            statuses=[TaskStatus.RUNNING] * 4 + [TaskStatus.SUCCESS],
        )
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(broadcast_results=False),
            observation_components_factory=observation_factory(events),
        )
        result = runner.run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            [
                entry
                for entry in runner.memory.prompt_history("a0", team_limit=20, system_limit=20)
                if entry["content"].get("result_broadcast")
            ],
            [],
        )

    def test_system_message_worker_is_disabled_by_default(self) -> None:
        class CountingManager:
            def __init__(self) -> None:
                self.calls = 0

            def system_message(self, context):
                self.calls += 1
                return "unused"

        events: list[str] = []
        agents = [FakeAgent("a0", events)]
        session = FakeSession(
            agents,
            events,
            statuses=[TaskStatus.SUCCESS],
        )
        manager = CountingManager()
        session.manager = manager
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
            observation_components_factory=observation_factory(events),
        )

        runner.run()

        self.assertEqual(manager.calls, 0)
        self.assertIsNone(runner._system_message_worker)

    def test_task_perception_rule_chain_consumes_raw_perception(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events)]
        session = FakeSession(agents, events)
        session.spec = create_default_registry().get(
            "monster_clearance"
        )
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
            observation_components_factory=observation_factory(events),
        )
        rule = runner._rule_factory()()
        context = RuleContext(
            mode="task",
            sub_mode="flat",
            radius=20.0,
            agent_name="a0",
        )
        raw = {
            "self": {
                "position": {"x": 0, "y": 0, "z": 0},
                "health": 20,
                "hunger": 20,
                "inventory": [],
                "equipment": [],
            },
            "blocks": [],
            "entities": [],
            "metadata": {"mode": "task", "subMode": "flat", "radius": 20.0},
        }
        result = rule.apply(raw, context)
        self.assertEqual(result.get("category"), "monster_clearance")
        self.assertIsInstance(result.get("sections"), dict)

    def test_failure_and_timeout_are_terminal_results(self) -> None:
        for terminal in (TaskStatus.FAILURE, TaskStatus.TIMEOUT):
            with self.subTest(terminal=terminal):
                events: list[str] = []
                agents = [FakeAgent("a0", events)]
                runner = PPARunner(
                    session=FakeSession(agents, events, statuses=[terminal]),
                    llm_client=FakeLLM(),
                    config=config(),
                    observation_components_factory=observation_factory(events),
                )
                result = runner.run()
                self.assertEqual(result.outcome.status, terminal)
                self.assertEqual(result.exit_code, 1)

    def test_planning_failures_do_not_stop_the_run(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events)
        llm = FakeLLM(responses={"a0": ["invalid", "still invalid"]})
        runner = PPARunner(
            session=FakeSession(
                [agent],
                events,
                terminal_condition=lambda: bool(agent.action_planner.executed),
            ),
            llm_client=llm,
            config=config(),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertIsNone(result.runner_error)
        self.assertEqual(result.worker_errors, [])
        self.assertGreaterEqual(llm.calls["a0"], 3)
        self.assertEqual(result.exit_code, 0)

    def test_initial_observation_failure_rolls_back_everything(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events)
        runtime_close_count = []
        session = FakeSession([agent], events)
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
            runtime_closer=lambda: runtime_close_count.append(True),
            observation_components_factory=observation_factory(events, {"a0"}),
        )

        with self.assertRaisesRegex(RuntimeError, "initial snapshot"):
            runner.start()

        self.assertEqual(session.close_count, 1)
        self.assertEqual(len(runtime_close_count), 1)
        self.assertTrue(runner.status()["closed"])
        self.assertIn("startup failed", runner.status()["runner_error"])

    def test_keyboard_interrupt_repeat_run_and_idempotent_close(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events)
        session = FakeSession([agent], events, poll_exception=KeyboardInterrupt())
        runner = PPARunner(
            session=session,
            llm_client=FakeLLM(),
            config=config(),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()
        runner.close()

        self.assertTrue(result.interrupted)
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(session.close_count, 1)
        with self.assertRaisesRegex(RuntimeError, "only start once"):
            runner.run()

    def test_cleanup_stops_continuous_action_before_session(self) -> None:
        events: list[str] = []
        agent = FakeAgent("a0", events, keep_running=True)
        llm = FakeLLM(
            responses={
                "a0": [
                    '{"skill": "wait.wait(seconds=10)", "interrupt": false, '
                    '"reason": "hold"}'
                ]
            },
            delay=0.005,
        )
        session = FakeSession(
            [agent],
            events,
            terminal_condition=lambda: bool(agent.action_planner.started),
        )
        runner = PPARunner(
            session=session,
            llm_client=llm,
            config=config(),
            runtime_closer=lambda: events.append("runtime.close"),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.outcome.status, TaskStatus.SUCCESS)
        self.assertLess(events.index("a0.stop:runner_stopped"), events.index("session.close"))
        self.assertLess(events.index("session.close"), events.index("runtime.close"))

    def test_two_agent_latest_buffer_interrupt_acceptance(self) -> None:
        events: list[str] = []
        a0 = FakeAgent("a0", events, keep_running=True)
        a1 = FakeAgent("a1", events)
        support_decision = (
            '{"skill": "chat.say(message=\'support\')", "interrupt": false, '
            '"reason": "support"}'
        )
        responses = {
            "a0": [
                '{"skill": "wait.wait(seconds=10)", "interrupt": false, '
                '"reason": "start"}',
                '{"skill": "chat.say(message=\'obsolete\')", "interrupt": false, '
                '"reason": "ordinary"}',
                '{"skill": "chat.say(message=\'latest\')", "interrupt": true, '
                '"reason": "urgent"}',
            ],
            "a1": [support_decision] * 50,
        }
        llm = FakeLLM(responses=responses, delay=0.008, default_text="invalid")
        session = FakeSession(
            [a0, a1],
            events,
            terminal_condition=lambda: (
                bool(a0.action_planner.stopped)
                and any(
                    unit.params.get("message") == "latest"
                    for unit in a0.action_planner.executed
                )
            ),
        )
        runner = PPARunner(
            session=session,
            llm_client=llm,
            config=config(
                task_poll_interval=0.001,
                max_consecutive_errors=100,
            ),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(
            a0.action_planner.stopped[0][1],
            "planning_interrupt",
        )
        self.assertEqual(
            [unit.params["message"] for unit in a0.action_planner.executed],
            ["latest"],
        )
        self.assertTrue(
            all(
                not worker["running"]
                for agent in runner.status()["agents"].values()
                for worker in agent.values()
            )
        )


class RunBudgetTests(unittest.TestCase):
    def test_no_caps_always_acquire(self) -> None:
        budget = RunBudget()
        for _ in range(5):
            self.assertTrue(budget.try_acquire("a0"))
        self.assertFalse(budget.exhausted)
        self.assertEqual(budget.team_used, 5)

    def test_team_cap(self) -> None:
        budget = RunBudget(team_cap=2)
        self.assertTrue(budget.try_acquire("a0"))
        self.assertTrue(budget.try_acquire("a1"))
        self.assertFalse(budget.try_acquire("a2"))
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.team_used, 2)

    def test_per_agent_cap(self) -> None:
        budget = RunBudget(per_agent_cap=1)
        self.assertTrue(budget.try_acquire("a0"))
        self.assertFalse(budget.try_acquire("a0"))
        self.assertTrue(budget.try_acquire("a1"))
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.used_by_agent("a0"), 1)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            RunBudget(team_cap=0)
        with self.assertRaises(ValueError):
            RunBudget(per_agent_cap=0)


class PPARunControlsTests(unittest.TestCase):
    def test_config_validates_budget_fields(self) -> None:
        for kwargs in (
            {"max_run_seconds": 0},
            {"max_llm_calls": 0},
            {"max_llm_calls_per_agent": 0},
            {"system_message_interval": 0},
            {"system_message_interval": True},
        ):
            with self.assertRaises(ValueError):
                PPAConfig(**kwargs)
        with self.assertRaises(ValueError):
            PPAConfig(broadcast_results="yes")
        with self.assertRaises(ValueError):
            PPAConfig(system_messages_enabled="yes")

    def test_max_llm_calls_stops_team(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
        llm = FakeLLM()
        session = FakeSession(agents, events)
        runner = PPARunner(
            session=session,
            llm_client=llm,
            config=config(max_llm_calls=2),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.stop_reason, "max_llm_calls")
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.runner_error)
        self.assertEqual(result.worker_errors, [])
        self.assertLessEqual(sum(llm.calls.values()), 2)
        self.assertEqual(session.close_count, 1)

    def test_max_llm_calls_per_agent_stops_team(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
        llm = FakeLLM()
        session = FakeSession(agents, events)
        runner = PPARunner(
            session=session,
            llm_client=llm,
            config=config(max_llm_calls_per_agent=1),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.stop_reason, "max_llm_calls")
        self.assertEqual(result.exit_code, 1)
        self.assertLessEqual(llm.calls.get("a0", 0), 1)
        self.assertLessEqual(llm.calls.get("a1", 0), 1)
        self.assertEqual(session.close_count, 1)

    def test_max_run_seconds_watchdog(self) -> None:
        events: list[str] = []
        agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
        llm = FakeLLM()
        session = FakeSession(agents, events)
        runner = PPARunner(
            session=session,
            llm_client=llm,
            config=config(max_run_seconds=0.2),
            observation_components_factory=observation_factory(events),
        )

        result = runner.run()

        self.assertEqual(result.stop_reason, "max_run_seconds")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(session.close_count, 1)


class PPATraceabilityTests(unittest.TestCase):
    def test_events_correlate_request_id_and_buffer_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = RecordingManager(
                RecordingConfig(root_dir=Path(tmp))
            ).start_run("parallelized")
            episode = run.start_episode(metadata={"task_id": "ppa.runner.test"})
            events: list[str] = []
            agents = [FakeAgent("a0", events), FakeAgent("a1", events)]
            llm = FakeLLM(default_text=CONTINUOUS_TEXT)
            closed: list[str] = []
            runner = PPARunner(
                session=FakeSession(
                    agents,
                    events,
                    statuses=[TaskStatus.RUNNING] * 8 + [TaskStatus.SUCCESS],
                ),
                llm_client=llm,
                config=config(),
                episode_recorder=episode,
                runtime_closer=lambda: closed.append("runtime.closed"),
                observation_components_factory=observation_factory(events),
            )

            result = runner.run()
            recorded = []
            with episode.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    recorded.append(json.loads(line))

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stop_reason, "task_outcome")
            self.assertEqual(result.cleanup_errors, [])
            self.assertEqual(closed, ["runtime.closed"])

            proposals = [
                item for item in recorded if item["type"] == "proposal.created"
            ]
            starts = [item for item in recorded if item["type"] == "action.started"]
            finished = [item for item in recorded if item["type"] == "action.finished"]
            statuses = [item for item in recorded if item["type"] == "action.status"]
            self.assertGreaterEqual(len(proposals), 2)
            self.assertGreaterEqual(len(starts), 2)
            self.assertGreaterEqual(len(finished), 1)
            self.assertGreaterEqual(len(statuses), 1)
            self.assertEqual({item["data"]["agent_id"] for item in starts}, {"a0", "a1"})

            request_ids = {request.request_id for request in llm.requests}
            versions_by_agent: dict[str, list[int]] = {}
            for item in proposals:
                self.assertIn(item["data"]["request_id"], request_ids)
                self.assertIsInstance(item["data"]["buffer_version"], int)
                versions_by_agent.setdefault(
                    item["data"]["agent_id"], []
                ).append(item["data"]["buffer_version"])
            for agent_versions in versions_by_agent.values():
                self.assertEqual(agent_versions, sorted(agent_versions))
                self.assertEqual(
                    len(agent_versions),
                    len(set(agent_versions)),
                )

            proposals_by_plan = {
                item["data"]["plan_id"]: item["data"] for item in proposals
            }
            for item in starts:
                plan = item["data"]["plan_id"]
                self.assertIn(plan, proposals_by_plan)
                self.assertEqual(
                    item["data"]["request_id"],
                    proposals_by_plan[plan]["request_id"],
                )
                self.assertIsInstance(item["data"]["buffer_version"], int)
            for item in finished:
                self.assertIn(item["data"]["request_id"], request_ids)

            cleanup_workers = [
                item
                for item in recorded
                if item["type"] == "cleanup.worker_stopped"
            ]
            teardowns = [
                item
                for item in recorded
                if item["type"] == "cleanup.acting_teardown"
            ]
            session_cleanup = [
                item
                for item in recorded
                if item["type"] == "cleanup.session"
            ]
            runtime_cleanup = [
                item
                for item in recorded
                if item["type"] == "cleanup.runtime"
            ]
            self.assertEqual(len(cleanup_workers), 6)
            self.assertTrue(all(item["data"]["ok"] for item in cleanup_workers))
            self.assertEqual(len(teardowns), 2)
            self.assertTrue(all(item["data"]["ok"] for item in teardowns))
            self.assertEqual(len(session_cleanup), 1)
            self.assertTrue(session_cleanup[0]["data"]["ok"])
            self.assertEqual(len(runtime_cleanup), 1)
            self.assertTrue(runtime_cleanup[0]["data"]["ok"])


if __name__ == "__main__":
    unittest.main()
