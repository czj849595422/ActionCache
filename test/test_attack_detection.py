from __future__ import annotations

import unittest

from src.perception.attack_detection import (
    AttackDetectionTracker,
    with_detection_context,
)


class AttackDetectionTests(unittest.TestCase):
    def test_first_observation_establishes_baseline(self) -> None:
        tracker = AttackDetectionTracker()

        initial = tracker.observe(
            {
                "self": {
                    "attackEventSequence": 0,
                    "lastAttackAt": None,
                    "health": 20,
                }
            },
            observed_at=1.0,
        )

        self.assertFalse(initial.received_attack)
        self.assertEqual(initial.received_attack_count, 0)
        self.assertEqual(initial.health_delta, 0.0)
        self.assertEqual(initial.baseline_event_sequence, 0)
        self.assertEqual(initial.baseline_health, 20.0)

        result = tracker.observe(
            {
                "self": {
                    "attackEventSequence": 2,
                    "lastAttackAt": 2_000.0,
                    "health": 17.5,
                }
            },
            observed_at=2.0,
        )

        self.assertTrue(result.received_attack)
        self.assertEqual(result.received_attack_count, 2)
        self.assertEqual(result.health_delta, -2.5)

    def test_event_after_successful_boundary_is_reported(self) -> None:
        tracker = AttackDetectionTracker()
        tracker.observe(
            {"self": {"attackEventSequence": 4, "health": 20}},
            observed_at=1.0,
        )
        tracker.mark_success()

        result = tracker.observe(
            {
                "self": {
                    "attackEventSequence": 5,
                    "lastAttackAt": 2_000.0,
                    "health": 17.5,
                }
            },
            observed_at=2.0,
        )

        self.assertTrue(result.received_attack)
        self.assertEqual(result.baseline_event_sequence, 4)
        self.assertEqual(result.observed_event_sequence, 5)
        self.assertEqual(result.baseline_health, 20.0)
        self.assertEqual(result.observed_health, 17.5)
        self.assertEqual(result.health_delta, -2.5)
        self.assertEqual(result.received_attack_count, 1)

    def test_event_before_successful_boundary_is_not_reported(self) -> None:
        tracker = AttackDetectionTracker()
        tracker.observe(
            {"self": {"attackEventSequence": 4, "health": 20}},
            observed_at=1.0,
        )
        tracker.mark_success()

        result = tracker.observe(
            {
                "sections": {
                    "self_state": {"facts": [{"attack_event_sequence": 4, "health": 20}]}
                }
            },
            observed_at=2.0,
        )

        self.assertFalse(result.received_attack)
        self.assertEqual(result.health_delta, 0.0)
        self.assertEqual(result.received_attack_count, 0)

    def test_health_increase_is_reported_as_positive_delta(self) -> None:
        tracker = AttackDetectionTracker()
        tracker.observe({"self": {"health": 8.0}}, observed_at=1.0)
        tracker.mark_success()

        result = tracker.observe({"self": {"health": 12.25}}, observed_at=2.0)

        self.assertEqual(result.health_delta, 4.25)

    def test_success_boundary_resets_health_delta(self) -> None:
        tracker = AttackDetectionTracker()
        tracker.observe(
            {"self": {"health": 20.0, "attackEventSequence": 0}},
            observed_at=1.0,
        )
        tracker.mark_success()
        tracker.observe(
            {"self": {"health": 15.0, "attackEventSequence": 0}},
            observed_at=2.0,
        )

        result = tracker.mark_success()

        self.assertEqual(result.baseline_health, 15.0)
        self.assertEqual(result.health_delta, 0.0)
        self.assertEqual(result.received_attack_count, 0)

    def test_render_context_contains_latest_change_without_mutating_observation(self) -> None:
        tracker = AttackDetectionTracker()
        observation = {
            "sections": {
                "self_state": {
                    "facts": [
                        {
                            "agent_name": "Alice",
                            "health": 17.5,
                            "attack_event_sequence": 0,
                        }
                    ]
                }
            }
        }
        tracker.observe(observation, observed_at=1.0)
        tracker.mark_success()
        snapshot = tracker.observe(
            {
                "sections": {
                    "self_state": {
                        "facts": [
                            {
                                "agent_name": "Alice",
                                "health": 15.0,
                                "attack_event_sequence": 0,
                            }
                        ]
                    }
                }
            },
            observed_at=2.0,
        )

        rendered = with_detection_context(observation, snapshot)

        self.assertEqual(rendered["sections"]["self_state"]["facts"][0]["health_delta"], -2.5)
        self.assertEqual(rendered["sections"]["self_state"]["facts"][0]["recent_attack_count"], 0)
        self.assertNotIn("health_delta", observation["sections"]["self_state"]["facts"][0])

    def test_success_boundary_does_not_change_task_or_prompt_data(self) -> None:
        tracker = AttackDetectionTracker()
        tracker.observe(
            {"self": {"attackEventSequence": 0, "health": 20}},
            observed_at=1.0,
        )
        result = tracker.mark_success()

        self.assertEqual(result.to_dict()["received_attack"], False)
        self.assertEqual(result.to_dict()["health_delta"], 0.0)
        self.assertNotIn("prompt", result.to_dict())


if __name__ == "__main__":
    unittest.main()
