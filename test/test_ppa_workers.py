#!/usr/bin/env python3
"""Worker semantics tests for the PPA baseline."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import (  # noqa: E402
    ActionMode,
    ActionResult,
    ActionStatus,
    SimpleInstructionRegistry,
    SkillRegistry,
)
from src.baseline.Parallelized import (  # noqa: E402
    JsonDecisionCodec,
    PPAConfig,
    PPAProposal,
)
from src.baseline.Parallelized.budget import RunBudget  # noqa: E402
from src.baseline.Parallelized.workers import (  # noqa: E402
    ActingWorker,
    ObservationWorker,
    PlanningWorker,
)
from src.interface import ActionResultEnvelope, ActionUnit  # noqa: E402
from src.llm import LLMRequest  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer  # noqa: E402


class FakePerception:
    def __init__(self, values) -> None:
        self.values = iter(values)
        self.rule = None

    def perceive(self):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class FakeVerbalizer:
    def render(self, value) -> str:
        return f"observation: {value['value']}"


class CountingVerbalizer:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, value) -> str:
        self.calls += 1
        return "unused"


class FakePromptBuilder:
    def __init__(self) -> None:
        self.build_kwargs = []

    def build(self, **kwargs) -> LLMRequest:
        self.build_kwargs.append(dict(kwargs))
        return LLMRequest(messages=({"role": "user", "content": "plan"},))

    def build_passive(self, **kwargs) -> LLMRequest:
        return LLMRequest(messages=({"role": "user", "content": "passive"},))


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return SimpleNamespace(content=self.responses.pop(0))


class FakePlanner:
    def __init__(self) -> None:
        self.registry = SkillRegistry(
            allowed_actions={"wait.wait", "chat.say", "chat.whisper"}
        )
        self.started: list[ActionUnit] = []
        self.executed: list[ActionUnit] = []
        self.stopped: list[tuple[str, str]] = []
        self.status_result = ActionResult(
            "wait.wait",
            True,
            action_id="active-1",
            status=ActionStatus.RUNNING,
        )
        self.execute_hook = None
        self.execute_ok = True
        self.stop_ok = True

    def start_unit(self, unit: ActionUnit) -> ActionResultEnvelope:
        self.started.append(unit)
        return ActionResultEnvelope(
            action=unit.action,
            ok=True,
            agent_id=unit.agent_id,
            action_id="active-1",
            status=ActionStatus.RUNNING,
        )

    def execute_unit(self, unit: ActionUnit) -> ActionResultEnvelope:
        self.executed.append(unit)
        if self.execute_hook is not None:
            self.execute_hook()
        return ActionResultEnvelope(
            action=unit.action,
            ok=self.execute_ok,
            agent_id=unit.agent_id,
            status=(ActionStatus.COMPLETED if self.execute_ok else ActionStatus.FAILED),
            error=None if self.execute_ok else "skill failed",
            result={"message": unit.params.get("message")},
        )

    def status(self, action_id: str) -> ActionResult:
        return self.status_result

    def stop(
        self,
        action_id: str,
        *,
        reason: str,
        timeout: float | None = None,
    ) -> ActionResult:
        self.stopped.append((action_id, reason))
        return ActionResult(
            "wait.wait",
            self.stop_ok,
            action_id=action_id,
            status=ActionStatus.STOPPED if self.stop_ok else ActionStatus.FAILED,
            error=None if self.stop_ok else "stop failed",
        )


class FakeAgent:
    def __init__(self, username: str = "a0") -> None:
        self.username = username
        self.action_planner = FakePlanner()


class FailingStatusPollPlanner(FakePlanner):
    def status(self, action_id: str) -> ActionResult:
        raise RuntimeError("bridge status unavailable")


def proposal(
    memory: TeamMemory,
    *,
    action: str,
    params: dict,
    mode: str,
    interrupt: bool = False,
) -> PPAProposal:
    item = PPAProposal(
        action_unit=ActionUnit(
            agent_id="a0",
            action=action,
            params=params,
            mode=mode,
        ),
        interrupt=interrupt,
        reason="test",
        passive_message=None,
        raw_text="test",
    )
    entry = memory.append_history_event(
        agent_id="a0", source="self", event_type="planning",
        content={
            "standard_plan": action,
            "selected_action": action,
            "reasoning": "test",
            "result_status": None,
        },
        metadata={"status": "planned", "interrupt": interrupt},
    )
    item.memory_entry_id = entry.entry_id
    return item


class ObservationWorkerTests(unittest.TestCase):
    def test_success_overwrites_text_and_failures_keep_last_observation(self) -> None:
        memory = TeamMemory()
        worker = ObservationWorker(
            agent_id="a0",
            perception=FakePerception(
                [{"value": 1}, RuntimeError("x"), RuntimeError("y"), RuntimeError("z")]
            ),
            verbalizer=FakeVerbalizer(),
            memory=memory,
            config=PPAConfig(max_consecutive_errors=3),
            rule_factory=lambda: "rule",
        )
        worker.iterate()
        self.assertEqual(memory.get_observation("a0").content, "observation: 1")
        worker.iterate()
        worker.iterate()
        self.assertEqual(memory.get_observation("a0").content, "observation: 1")
        with self.assertRaisesRegex(RuntimeError, "failed 3 times"):
            worker.iterate()

    def test_dead_observation_marks_not_live_and_skips_memory_write(self) -> None:
        agent = FakeAgent("a0")
        gate = EntityLivenessGate([agent])
        memory = TeamMemory()
        verbalizer = CountingVerbalizer()
        worker = ObservationWorker(
            agent_id="a0",
            perception=FakePerception([{"self": {"health": 0}}]),
            verbalizer=verbalizer,
            memory=memory,
            config=PPAConfig(),
            liveness_gate=gate,
        )

        worker.iterate()

        self.assertFalse(agent.live)
        self.assertIsNone(memory.get_observation("a0"))
        self.assertEqual(verbalizer.calls, 0)


class PlanningWorkerTests(unittest.TestCase):
    def make_worker(self, responses: list[str], memory=None, buffer=None):
        memory = memory or TeamMemory()
        memory.update_observation("a0", "ready")
        buffer = buffer or SingleSlotOverwriteBuffer()
        llm = FakeLLM(responses)
        worker = PlanningWorker(
            agent=FakeAgent(),
            teammates=["a1"],
            llm_client=llm,
            prompt_builder=FakePromptBuilder(),
            codec=JsonDecisionCodec(),
            buffer=buffer,
            interrupt_event=threading.Event(),
            memory=memory,
            config=PPAConfig(),
        )
        return worker, memory, buffer, llm

    def test_latest_proposal_overwrites_buffer(self) -> None:
        first = (
            '{"skill": "wait.wait(seconds=1)", "interrupt": false, '
            '"reason": "first"}'
        )
        second = (
            '{"skill": "chat.say(message=\'done\')", "interrupt": true, '
            '"reason": "second"}'
        )
        worker, memory, buffer, _ = self.make_worker([first, second])
        worker.iterate()
        first_entry = memory.prompt_history("a0")[-1]
        worker.iterate()

        latest = buffer.peek()
        self.assertEqual(latest[1].action_unit.action, "chat.say")
        self.assertTrue(worker.interrupt_event.is_set())
        self.assertEqual(memory.get(first_entry["entry_id"]).metadata["status"], "superseded")
        # 被动式通信尚未接入 JSON 协议（后续单独开发），此时不应产生 chat 记录。
        self.assertEqual(
            [entry for entry in memory.prompt_history("a0") if entry["event_type"] == "communication"],
            [],
        )

    def test_parse_failure_does_not_write_buffer(self) -> None:
        worker, _, buffer, _ = self.make_worker(["invalid"])
        worker.iterate()
        self.assertIsNone(buffer.peek())

    def test_planning_failures_are_retried_and_feedback_resets_at_threshold(self) -> None:
        valid = (
            '{"skill": "wait.wait(seconds=1)", "interrupt": false, '
            '"reason": "recovered"}'
        )
        worker, _, buffer, llm = self.make_worker(
            ["invalid-1", "invalid-2", "invalid-3", valid]
        )
        prompt_builder = worker.prompt_builder

        for _ in range(4):
            worker.iterate()

        self.assertEqual(llm.calls, 4)
        self.assertIsNotNone(buffer.peek())
        self.assertEqual(worker.consecutive_errors, 0)
        self.assertEqual(worker.last_prompt_error, None)
        self.assertEqual(worker.planning_error_history, [])
        self.assertEqual(prompt_builder.build_kwargs[0]["error_history"], ())
        self.assertEqual(len(prompt_builder.build_kwargs[1]["error_history"]), 1)
        self.assertEqual(len(prompt_builder.build_kwargs[2]["error_history"]), 2)
        self.assertEqual(prompt_builder.build_kwargs[3]["error_history"], ())

    def test_passive_communication_fires_once_per_new_result(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "wait", "result_description": "a0：执行wait(seconds=1) 完成", "result_status": "completed"})
        plan = (
            '{"skill": "wait.wait(seconds=1)", "interrupt": false, '
            '"reason": "r"}'
        )
        llm = FakeLLM(["team note", plan, plan, plan])
        worker = PlanningWorker(
            agent=FakeAgent(),
            teammates=["a1"],
            llm_client=llm,
            prompt_builder=FakePromptBuilder(),
            codec=JsonDecisionCodec(),
            buffer=SingleSlotOverwriteBuffer(),
            interrupt_event=threading.Event(),
            memory=memory,
            config=PPAConfig(),
        )
        worker.iterate()  # 被动通信 + 本轮规划
        worker.iterate()  # 仅规划（不重复通信）
        worker.iterate()
        passive = [
            entry["content"]["message"]
            for entry in memory.prompt_history("a0")
            if entry["event_type"] == "communication" and entry["content"].get("message") in {"team note", "note-1", "note-2"}
        ]
        self.assertEqual(passive, ["team note"])
        self.assertEqual(llm.calls, 4)

    def test_passive_communication_fires_for_each_new_result(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        plan = (
            '{"skill": "wait.wait(seconds=1)", "interrupt": false, '
            '"reason": "r"}'
        )
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "wait", "result_description": "result-1", "result_status": "completed"})
        llm = FakeLLM(["note-1", plan, "note-2", plan, plan])
        worker = PlanningWorker(
            agent=FakeAgent(),
            teammates=["a1"],
            llm_client=llm,
            prompt_builder=FakePromptBuilder(),
            codec=JsonDecisionCodec(),
            buffer=SingleSlotOverwriteBuffer(),
            interrupt_event=threading.Event(),
            memory=memory,
            config=PPAConfig(),
        )
        worker.iterate()  # note-1 + plan
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "wait", "result_description": "result-2", "result_status": "completed"})
        worker.iterate()  # note-2 + plan
        worker.iterate()  # 仅规划
        passive = [
            entry["content"]["message"]
            for entry in memory.prompt_history("a0")
            if entry["event_type"] == "communication" and entry["content"].get("message") in {"note-1", "note-2"}
        ]
        self.assertEqual(passive, ["note-1", "note-2"])

    def test_passive_communication_counts_toward_budget(self) -> None:
        memory = TeamMemory()
        memory.update_observation("a0", "ready")
        memory.append_history_event(agent_id="a0", source="self", event_type="execution", content={"selected_action": "wait", "result_description": "result-1", "result_status": "completed"})
        plan = (
            '{"skill": "wait.wait(seconds=1)", "interrupt": false, '
            '"reason": "r"}'
        )
        llm = FakeLLM(["note-1", plan])
        worker = PlanningWorker(
            agent=FakeAgent(),
            teammates=["a1"],
            llm_client=llm,
            prompt_builder=FakePromptBuilder(),
            codec=JsonDecisionCodec(),
            buffer=SingleSlotOverwriteBuffer(),
            interrupt_event=threading.Event(),
            memory=memory,
            config=PPAConfig(),
            budget=RunBudget(team_cap=1),
        )
        worker.iterate()
        passive = [
            entry["content"]["message"]
            for entry in memory.prompt_history("a0")
            if entry["event_type"] == "communication" and entry["content"].get("message")
        ]
        self.assertEqual(passive, ["note-1"])
        self.assertEqual(llm.calls, 1)
        self.assertTrue(worker.budget.exhausted)


class ActingWorkerTests(unittest.TestCase):
    def make_worker(self):
        memory = TeamMemory()
        agent = FakeAgent()
        buffer = SingleSlotOverwriteBuffer()
        event = threading.Event()
        worker = ActingWorker(
            agent=agent,
            buffer=buffer,
            interrupt_event=event,
            memory=memory,
            config=PPAConfig(),
        )
        return worker, agent.action_planner, memory, buffer, event

    def test_continuous_interrupt_stops_old_and_executes_only_latest(self) -> None:
        worker, planner, memory, buffer, event = self.make_worker()
        current = proposal(
            memory,
            action="wait.wait",
            params={"seconds": 10},
            mode=ActionMode.CONTINUOUS,
        )
        ordinary = proposal(
            memory,
            action="chat.say",
            params={"message": "obsolete"},
            mode=ActionMode.ONESHOT,
        )
        latest = proposal(
            memory,
            action="chat.say",
            params={"message": "latest"},
            mode=ActionMode.ONESHOT,
            interrupt=True,
        )
        buffer.put(current)
        worker.iterate()
        buffer.put(ordinary)
        worker.iterate()
        self.assertEqual(planner.executed, [])
        buffer.put(latest)
        event.set()
        worker.iterate()
        worker.iterate()

        self.assertEqual(planner.stopped, [("active-1", "planning_interrupt")])
        self.assertEqual([item.params["message"] for item in planner.executed], ["latest"])
        self.assertEqual(
            memory.get(current.memory_entry_id).metadata["status"],
            "interrupted",
        )

    def test_continuous_stop_failure_is_fatal(self) -> None:
        worker, planner, memory, buffer, event = self.make_worker()
        current = proposal(
            memory,
            action="wait.wait",
            params={"seconds": 10},
            mode=ActionMode.CONTINUOUS,
        )
        buffer.put(current)
        worker.iterate()
        planner.stop_ok = False
        event.set()
        with self.assertRaisesRegex(RuntimeError, "failed to interrupt"):
            worker.iterate()
        self.assertEqual(planner.executed, [])

    def test_continuous_status_failures_release_active_action(self) -> None:
        worker, _, memory, buffer, _ = self.make_worker()
        worker.planner = FailingStatusPollPlanner()
        current = proposal(
            memory,
            action="wait.wait",
            params={"seconds": 10},
            mode=ActionMode.CONTINUOUS,
        )
        buffer.put(current)
        worker.iterate()

        for _ in range(worker.config.max_consecutive_errors):
            worker.iterate()

        self.assertIsNone(worker.active_proposal)
        self.assertEqual(
            worker.planner.stopped,
            [("active-1", "status_poll_failed")],
        )
        self.assertEqual(
            memory.get(current.memory_entry_id).metadata["status"],
            "failed",
        )

    def test_oneshot_interrupt_is_deferred_without_stop(self) -> None:
        worker, planner, memory, buffer, event = self.make_worker()
        current = proposal(
            memory,
            action="chat.say",
            params={"message": "first"},
            mode=ActionMode.ONESHOT,
        )
        latest = proposal(
            memory,
            action="chat.say",
            params={"message": "latest"},
            mode=ActionMode.ONESHOT,
            interrupt=True,
        )
        buffer.put(current)

        def publish_interrupt() -> None:
            buffer.put(latest)
            event.set()

        planner.execute_hook = publish_interrupt
        worker.iterate()
        planner.execute_hook = None
        self.assertEqual(planner.stopped, [])
        self.assertEqual(worker.deferred_interrupts, 1)
        self.assertEqual(
            memory.get(latest.memory_entry_id).metadata["interrupt_state"],
            "deferred",
        )
        worker.iterate()
        self.assertEqual(
            [item.params["message"] for item in planner.executed],
            ["first", "latest"],
        )

    def test_successful_active_chat_and_skill_failure_are_written_back(self) -> None:
        worker, planner, memory, buffer, _ = self.make_worker()
        chat = proposal(
            memory,
            action="chat.whisper",
            params={"username": "a1", "message": "hello"},
            mode=ActionMode.ONESHOT,
        )
        buffer.put(chat)
        worker.iterate()
        chat_entry = memory.get(chat.memory_entry_id)
        self.assertEqual(chat_entry.metadata["recipient"], "a1")
        self.assertEqual(chat_entry.content["communication"], "hello")

        failed = proposal(
            memory,
            action="chat.say",
            params={"message": "fail"},
            mode=ActionMode.ONESHOT,
        )
        planner.execute_ok = False
        buffer.put(failed)
        worker.iterate()
        entry = memory.get(failed.memory_entry_id)
        self.assertEqual(entry.metadata["status"], "failed")
        self.assertEqual(entry.content["result_data"]["error"], "skill failed")
        self.assertIsNone(worker.active_proposal)

    def make_broadcast_worker(self, *, broadcast_results: bool = True):
        memory = TeamMemory()
        agent = FakeAgent()
        buffer = SingleSlotOverwriteBuffer()
        event = threading.Event()
        broadcasts: list[str] = []
        worker = ActingWorker(
            agent=agent,
            buffer=buffer,
            interrupt_event=event,
            memory=memory,
            config=PPAConfig(),
            instructions=SimpleInstructionRegistry(),
            broadcast=broadcasts.append,
            broadcast_results=broadcast_results,
        )
        return worker, agent, memory, buffer, event, broadcasts

    def _result_texts(self, memory: TeamMemory) -> list[str]:
        return [
            entry["content"]["result_broadcast"]
            for entry in memory.prompt_history("a0")
            if entry["content"].get("result_broadcast")
        ]

    def test_result_feedback_recorded_on_oneshot_success(self) -> None:
        worker, agent, memory, buffer, _, broadcasts = self.make_broadcast_worker()
        item = proposal(
            memory,
            action="chat.say",
            params={"message": "hi"},
            mode=ActionMode.ONESHOT,
        )
        item.input_params = {"message": "hi"}
        buffer.put(item)
        worker.iterate()

        expected = "a0：执行chat(message='hi') 完成"
        self.assertEqual(self._result_texts(memory), [expected])
        self.assertEqual(broadcasts, [expected])
        self.assertEqual(len(memory.prompt_history("a0")), 1)

    def test_result_feedback_recorded_on_oneshot_failure(self) -> None:
        worker, agent, memory, buffer, _, broadcasts = self.make_broadcast_worker()
        agent.action_planner.execute_ok = False
        item = proposal(
            memory,
            action="chat.say",
            params={"message": "hi"},
            mode=ActionMode.ONESHOT,
        )
        item.input_params = {"message": "hi"}
        buffer.put(item)
        worker.iterate()

        expected = "a0：执行chat(message='hi') 失败：skill failed"
        self.assertEqual(
            self._result_texts(memory),
            [expected],
        )
        self.assertEqual(broadcasts, [expected])

    def test_result_feedback_recorded_on_continuous_interrupt(self) -> None:
        worker, agent, memory, buffer, event, broadcasts = self.make_broadcast_worker()
        item = proposal(
            memory,
            action="wait.wait",
            params={"seconds": 10},
            mode=ActionMode.CONTINUOUS,
        )
        item.input_params = {"seconds": 10}
        buffer.put(item)
        worker.iterate()
        event.set()
        worker.iterate()

        expected = "a0：执行wait(seconds=10) 中断"
        self.assertEqual(self._result_texts(memory), [expected])
        self.assertEqual(broadcasts, [expected])

    def test_result_feedback_deferred_on_oneshot(self) -> None:
        worker, agent, memory, buffer, event, broadcasts = self.make_broadcast_worker()
        item = proposal(
            memory,
            action="chat.say",
            params={"message": "hi"},
            mode=ActionMode.ONESHOT,
        )
        item.input_params = {"message": "hi"}
        buffer.put(item)
        agent.action_planner.execute_hook = event.set
        worker.iterate()

        self.assertTrue(
            any("（中断延后）" in text for text in self._result_texts(memory))
        )
        self.assertTrue(any("（中断延后）" in text for text in broadcasts))
        self.assertEqual(worker.deferred_interrupts, 1)

    def test_no_broadcast_on_teardown(self) -> None:
        worker, agent, memory, buffer, _, broadcasts = self.make_broadcast_worker()
        item = proposal(
            memory,
            action="wait.wait",
            params={"seconds": 10},
            mode=ActionMode.CONTINUOUS,
        )
        buffer.put(item)
        worker.iterate()
        worker.teardown()

        self.assertEqual(broadcasts, [])
        self.assertEqual(self._result_texts(memory), [])

    def test_broadcast_results_disabled_skips_result_record(self) -> None:
        worker, agent, memory, buffer, _, broadcasts = self.make_broadcast_worker(
            broadcast_results=False,
        )
        item = proposal(
            memory,
            action="chat.say",
            params={"message": "hi"},
            mode=ActionMode.ONESHOT,
        )
        item.input_params = {"message": "hi"}
        buffer.put(item)
        worker.iterate()

        self.assertEqual(broadcasts, [])
        self.assertEqual(self._result_texts(memory), [])


if __name__ == "__main__":
    unittest.main()
