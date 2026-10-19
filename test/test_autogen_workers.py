"""AutoGen baseline worker and runner assembly tests."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry, SkillRegistry  # noqa: E402
from src.baseline.AutoGen import (  # noqa: E402
    A2AStore,
    AutoGenCommanderWorker,
    AutoGenConfig,
    AutoGenRunner,
    AutoGenSubtaskPlanningWorker,
    SubtaskAssignment,
)
from src.baseline.BasicAsync.workers import BasicActingWorker  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.runtime import EntityLivenessGate, SingleSlotOverwriteBuffer  # noqa: E402


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        text = self.responses.pop(0)
        return SimpleNamespace(
            content=text,
            request_id=f"req-{len(self.calls)}",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self.executed = []

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
        self.executed.append(unit)
        return SimpleNamespace(
            ok=True,
            status="completed",
            action=unit.action,
            action_id="act-1",
            result={"ok": True, "status": "completed", "description": "ok"},
            raw={"ok": True},
        )

    def status(self, action_id: str):
        return SimpleNamespace(ok=True, status="completed", result={"ok": True}, raw={})

    def stop(self, action_id: str, reason: str = "stop"):
        return SimpleNamespace(ok=True, status="interrupted", result={"ok": True}, raw={})


class FakeAgent:
    def __init__(self, username: str, registry: SkillRegistry) -> None:
        self.username = username
        self.action_planner = FakePlanner(registry)


def mining_registry() -> SkillRegistry:
    from src.tasks import create_default_registry, task_action_space

    spec = create_default_registry().get("mining")
    return SkillRegistry(allowed_actions=task_action_space(spec))


def fake_spec():
    return SimpleNamespace(
        task_id="task.1",
        description="test task",
        category="mining",
        observation_range=8,
        load_description_text=lambda: "test task description",
    )


def make_assignment(agent_id: str = "Alice", rounds: int = 5) -> SubtaskAssignment:
    return SubtaskAssignment(
        recipient=agent_id,
        subtask_id="s1",
        objective="wait briefly",
        success_criteria="wait action succeeds",
        max_planning_rounds=rounds,
    )


class AutoGenWorkerTests(unittest.TestCase):
    def test_commander_replans_for_idle_agent_after_public_result_change(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "obs alice")
        memory.update_observation("Bob", "obs bob")
        a2a = A2AStore(agents=("Alice", "Bob"))
        llm = FakeLLM(
            [
                """
                <reason>start alice</reason>
                <assignments>[{
                  "recipient": "Alice",
                  "subtask_id": "s1",
                  "objective": "inspect first",
                  "success_criteria": "inspection done",
                  "constraints": []
                }]</assignments>
                """,
                """
                <reason>use new public result</reason>
                <assignments>[{
                  "recipient": "Bob",
                  "subtask_id": "s2",
                  "objective": "react to Alice result",
                  "success_criteria": "reaction done",
                  "constraints": []
                }]</assignments>
                """,
            ]
        )
        commander = AutoGenCommanderWorker(
            llm_client=llm,
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(),
            spec=fake_spec(),
            executors=("Alice", "Bob"),
        )

        commander.iterate()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(a2a.state("Alice").status, "assigned")
        self.assertEqual(a2a.state("Bob").status, "idle")

        commander.iterate()
        self.assertEqual(len(llm.calls), 1)

        memory.append_history_event(
            agent_id="Alice", source="teammate", event_type="execution",
            content={"selected_action": "inspect", "result_description": "Alice finished inspecting.", "result_status": "completed"},
        )
        commander.iterate()
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(a2a.state("Bob").status, "assigned")

    def test_commander_does_not_plan_for_dead_only_executor(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "obs alice")
        a2a = A2AStore(agents=("Alice",))
        agent = FakeAgent("Alice", mining_registry())
        gate = EntityLivenessGate([agent])
        gate.mark_live("Alice", False, reason="test")
        llm = FakeLLM(
            [
                """
                <reason>should not run</reason>
                <assignments>[]</assignments>
                """
            ]
        )
        commander = AutoGenCommanderWorker(
            llm_client=llm,
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(),
            spec=fake_spec(),
            executors=("Alice",),
            liveness_gate=gate,
        )

        commander.iterate()

        self.assertEqual(llm.calls, [])
        self.assertEqual(a2a.state("Alice").status, "idle")

    def test_commander_retries_after_error_threshold_without_fatal_stop(self) -> None:
        memory = TeamMemory()
        memory.update_observation("Alice", "obs alice")
        a2a = A2AStore(agents=("Alice",))
        valid = (
            "<reason>recovered</reason>"
            '<assignments>[{"recipient":"Alice","subtask_id":"s1",'
            '"objective":"wait","success_criteria":"done",'
            '"constraints":[]}]</assignments>'
        )
        llm = FakeLLM(["invalid-1", "invalid-2", "invalid-3", valid])
        commander = AutoGenCommanderWorker(
            llm_client=llm,
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(max_consecutive_planning_errors=3),
            spec=fake_spec(),
            executors=("Alice",),
        )

        for _ in range(4):
            commander.iterate()

        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(commander.planned_count, 1)
        self.assertEqual(commander.consecutive_errors, 0)
        self.assertEqual(commander.planning_error_history, [])
        self.assertIn("第 1 次失败：", llm.calls[1].messages[1]["content"])
        self.assertIn("第 2 次失败：", llm.calls[2].messages[1]["content"])
        self.assertNotIn("第 1 次失败：", llm.calls[3].messages[1]["content"])

    def test_executor_continue_publishes_action_and_public_result_after_acting(self) -> None:
        registry = mining_registry()
        agent = FakeAgent("Alice", registry)
        memory = TeamMemory()
        memory.update_observation("Alice", "obs")
        a2a = A2AStore(agents=("Alice",))
        a2a.assign(make_assignment())
        buffer = SingleSlotOverwriteBuffer()
        config = AutoGenConfig()
        acting = BasicActingWorker(
            agent=agent,
            buffer=buffer,
            memory=memory,
            config=config,
            instructions=SimpleInstructionRegistry(),
            broadcast=None,
            task_category="mining",
        )
        planning = AutoGenSubtaskPlanningWorker(
            agent=agent,
            teammates=(),
            llm_client=FakeLLM(
                [
                    "<status>continue</status><reason>need wait</reason>"
                    "<action>wait(seconds=1)</action>"
                ]
            ),
            buffer=buffer,
            memory=memory,
            a2a=a2a,
            config=config,
            registry=registry,
            spec=fake_spec(),
            instructions=SimpleInstructionRegistry(),
            acting_worker=acting,
        )
        planning.iterate()
        self.assertTrue(buffer.has_new(0))
        acting.iterate()
        result_history = memory.prompt_history("Commander", team_limit=20, system_limit=20)
        self.assertEqual(len([entry for entry in result_history if entry["source"] == "teammate"]), 1)
        self.assertEqual(a2a.commander_history()[0].message_type, "assignment")

    def test_executor_terminal_report_is_private(self) -> None:
        registry = mining_registry()
        agent = FakeAgent("Alice", registry)
        memory = TeamMemory()
        memory.update_observation("Alice", "obs")
        a2a = A2AStore(agents=("Alice",))
        a2a.assign(make_assignment())
        acting = SimpleNamespace(is_busy=False, seen_version=0)
        planning = AutoGenSubtaskPlanningWorker(
            agent=agent,
            teammates=(),
            llm_client=FakeLLM(
                [
                    "<status>completed</status><reason>done</reason>"
                    "<report>finished privately</report>"
                ]
            ),
            buffer=SingleSlotOverwriteBuffer(),
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(),
            registry=registry,
            spec=fake_spec(),
            instructions=SimpleInstructionRegistry(),
            acting_worker=acting,
        )
        planning.iterate()
        reports = [
            entry for entry in memory.prompt_history("Commander", team_limit=20, system_limit=20)
            if entry["source"] == "teammate"
        ]
        self.assertEqual(len(reports), 1)
        self.assertIn("状态：completed", reports[0]["content"]["message"])
        self.assertNotIn("finished privately", reports[0]["content"]["message"])
        history = a2a.commander_history()
        self.assertEqual(history[-1].message_type, "report")
        self.assertEqual(history[-1].content["report"], "finished privately")

    def test_executor_reports_max_rounds_after_default_five_turns(self) -> None:
        registry = mining_registry()
        agent = FakeAgent("Alice", registry)
        memory = TeamMemory()
        memory.update_observation("Alice", "obs")
        a2a = A2AStore(agents=("Alice",))
        a2a.assign(make_assignment(rounds=5))
        for _ in range(5):
            a2a.increment_planning_round("Alice")
        planning = AutoGenSubtaskPlanningWorker(
            agent=agent,
            teammates=(),
            llm_client=FakeLLM([]),
            buffer=SingleSlotOverwriteBuffer(),
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(),
            registry=registry,
            spec=fake_spec(),
            instructions=SimpleInstructionRegistry(),
            acting_worker=SimpleNamespace(is_busy=False, seen_version=0),
        )
        planning.iterate()
        self.assertEqual(a2a.commander_history()[-1].content["status"], "max_rounds")
        self.assertEqual(planning.report_count, 1)

    def test_executor_skips_when_acting_busy_or_buffer_has_unconsumed_item(self) -> None:
        registry = mining_registry()
        agent = FakeAgent("Alice", registry)
        memory = TeamMemory()
        memory.update_observation("Alice", "obs")
        a2a = A2AStore(agents=("Alice",))
        a2a.assign(make_assignment())
        buffer = SingleSlotOverwriteBuffer()
        llm = FakeLLM(["unused"])
        busy = SimpleNamespace(is_busy=True, seen_version=0)
        planning = AutoGenSubtaskPlanningWorker(
            agent=agent,
            teammates=(),
            llm_client=llm,
            buffer=buffer,
            memory=memory,
            a2a=a2a,
            config=AutoGenConfig(),
            registry=registry,
            spec=fake_spec(),
            instructions=SimpleInstructionRegistry(),
            acting_worker=busy,
        )
        planning.iterate()
        self.assertEqual(llm.calls, [])
        busy.is_busy = False
        buffer.put(object())
        planning.iterate()
        self.assertEqual(llm.calls, [])


class AutoGenRunnerAssemblyTests(unittest.TestCase):
    def test_from_bundle_does_not_create_commander_skill_agent(self) -> None:
        agents = [FakeAgent("Alice", mining_registry()), FakeAgent("Bob", mining_registry())]
        bundle = SimpleNamespace(
            spec=fake_spec(),
            manager=object(),
            server=object(),
            agents=agents,
            world_workspace=None,
            registry_file=Path("chests.json"),
            close_runtime=lambda: None,
        )
        runner = AutoGenRunner.from_bundle(
            bundle,
            llm_client=FakeLLM([]),
            config=AutoGenConfig(),
            output=lambda _: None,
        )
        self.assertEqual([agent.username for agent in runner.session.agents], ["Alice", "Bob"])
        self.assertEqual(runner.config.commander_id, "Commander")


if __name__ == "__main__":
    unittest.main()
