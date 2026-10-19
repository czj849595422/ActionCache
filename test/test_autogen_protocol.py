"""AutoGen baseline protocol and A2A tests."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.AutoGen import (  # noqa: E402
    A2AStore,
    AutoGenProtocolError,
    SubtaskReport,
    parse_commander_response,
    parse_executor_response,
)


class AutoGenProtocolTests(unittest.TestCase):
    def test_commander_parses_multiple_assignments_with_default_rounds(self) -> None:
        result = parse_commander_response(
            """
            <reason>split work</reason>
            <assignments>[
              {
                "recipient": "Alice",
                "subtask_id": "s1",
                "objective": "inspect nearby hostiles",
                "success_criteria": "hostiles are located",
                "constraints": ["stay safe"]
              },
              {
                "recipient": "Bob",
                "subtask_id": "s2",
                "objective": "collect dropped items",
                "success_criteria": "nearby useful drops are collected",
                "constraints": []
              }
            ]</assignments>
            """,
            eligible_recipients=("Alice", "Bob"),
            default_max_rounds=5,
        )
        self.assertEqual(len(result.assignments), 2)
        self.assertEqual(result.assignments[0].max_planning_rounds, 5)

    def test_commander_allows_long_assignment_payload(self) -> None:
        long_constraint = "keep sweeping the assigned area carefully " * 120
        result = parse_commander_response(
            f"""
            <reason>split work</reason>
            <assignments>[{{
              "recipient": "Alice",
              "subtask_id": "s1",
              "objective": "inspect nearby hostiles",
              "success_criteria": "hostiles are located",
              "constraints": ["{long_constraint}"]
            }}]</assignments>
            """,
            eligible_recipients=("Alice",),
            default_max_rounds=5,
        )
        self.assertEqual(len(result.assignments), 1)
        self.assertGreater(len(result.assignments[0].constraints[0]), 4000)

    def test_commander_rejects_busy_or_unknown_recipient(self) -> None:
        with self.assertRaises(AutoGenProtocolError):
            parse_commander_response(
                """
                <reason>bad</reason>
                <assignments>[{
                  "recipient": "Alice",
                  "subtask_id": "s1",
                  "objective": "work",
                  "success_criteria": "done",
                  "constraints": []
                }]</assignments>
                """,
                eligible_recipients=("Bob",),
                default_max_rounds=5,
            )

    def test_executor_continue_requires_exactly_one_action(self) -> None:
        decision = parse_executor_response(
            "<status>continue</status><reason>need move</reason>"
            "<action>wait(seconds=1)</action>"
        )
        self.assertEqual(decision.status, "continue")
        self.assertEqual(decision.action_text, "wait(seconds=1)")
        with self.assertRaises(AutoGenProtocolError):
            parse_executor_response(
                "<status>continue</status><reason>bad</reason>"
                "<action>wait(seconds=1)</action><action>wait(seconds=2)</action>"
            )

    def test_executor_terminal_requires_private_report_and_no_action(self) -> None:
        decision = parse_executor_response(
            "<status>completed</status><reason>done</reason>"
            "<report>subtask complete</report>"
        )
        self.assertEqual(decision.report, "subtask complete")
        with self.assertRaises(AutoGenProtocolError):
            parse_executor_response(
                "<status>completed</status><reason>bad</reason>"
                "<action>wait(seconds=1)</action><report>done</report>"
            )
        with self.assertRaises(AutoGenProtocolError):
            parse_executor_response(
                "<status>unknown</status><reason>bad</reason><report>x</report>"
            )


class A2AStoreTests(unittest.TestCase):
    def test_commander_sees_all_and_executors_see_only_own_thread(self) -> None:
        store = A2AStore(agents=("Alice", "Bob"))
        alice_assignment = parse_commander_response(
            """
            <reason>assign</reason>
            <assignments>[{
              "recipient": "Alice",
              "subtask_id": "s1",
              "objective": "work alice",
              "success_criteria": "done",
              "constraints": []
            }]</assignments>
            """,
            eligible_recipients=("Alice",),
            default_max_rounds=5,
        ).assignments[0]
        store.assign(alice_assignment)
        store.submit_report(
            SubtaskReport(
                sender="Alice",
                subtask_id="s1",
                status="completed",
                report="alice done",
            )
        )
        self.assertEqual(len(store.commander_history()), 2)
        self.assertEqual(len(store.executor_history("Alice")), 2)
        self.assertEqual(store.executor_history("Bob"), [])

    def test_assignment_state_allows_only_idle_or_reported_waiting(self) -> None:
        store = A2AStore(agents=("Alice",))
        assignment = parse_commander_response(
            """
            <reason>assign</reason>
            <assignments>[{
              "recipient": "Alice",
              "subtask_id": "s1",
              "objective": "work",
              "success_criteria": "done",
              "constraints": []
            }]</assignments>
            """,
            eligible_recipients=("Alice",),
            default_max_rounds=5,
        ).assignments[0]
        store.assign(assignment)
        self.assertEqual(store.eligible_recipients(), ())
        store.submit_report(
            SubtaskReport(
                sender="Alice",
                subtask_id="s1",
                status="blocked",
                report="blocked",
            )
        )
        self.assertEqual(store.eligible_recipients(), ("Alice",))


if __name__ == "__main__":
    unittest.main()
