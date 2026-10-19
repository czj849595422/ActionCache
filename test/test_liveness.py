"""Entity-backed agent liveness gate tests."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.runtime import (  # noqa: E402
    EntityLivenessGate,
    agent_is_live,
    snapshot_live_state,
)
from src.tasks import TaskOutcome, TaskStatus  # noqa: E402


class LivenessHelperTests(unittest.TestCase):
    def test_skill_agent_defaults_live_true(self) -> None:
        agent = SkillAgent(username="Alice", bridge_port=3999)

        self.assertTrue(agent.live)
        self.assertTrue(agent_is_live(agent))

    def test_skill_agent_bridge_session_ignores_environment_proxy(self) -> None:
        agent = SkillAgent(username="Alice", bridge_port=3999)

        self.assertFalse(agent._bridge_session.trust_env)

    def test_snapshot_marks_dead_health_as_not_live(self) -> None:
        self.assertFalse(snapshot_live_state({"self": {"health": 0}}))
        self.assertFalse(snapshot_live_state({"health": -1}))

    def test_snapshot_marks_spectator_as_not_live(self) -> None:
        self.assertFalse(
            snapshot_live_state({"self": {"gameMode": "spectator", "health": 20}})
        )
        self.assertFalse(snapshot_live_state({"gamemode": "Spectator"}))

    def test_gate_marks_eliminated_agents_not_live(self) -> None:
        alice = SimpleNamespace(username="Alice")
        bob = SimpleNamespace(username="Bob")
        gate = EntityLivenessGate([alice, bob])

        gate.update_from_metrics({"eliminated_agents": ["Alice"]})

        self.assertFalse(alice.live)
        self.assertTrue(gate.is_live("Bob"))

    def test_gate_marks_all_dead_outcome_not_live(self) -> None:
        agents = [SimpleNamespace(username="Alice"), SimpleNamespace(username="Bob")]
        gate = EntityLivenessGate(agents)

        gate.update_from_task_state(
            outcome=TaskOutcome(TaskStatus.FAILURE, reason="all_agents_dead"),
            metrics={},
        )

        self.assertFalse(any(agent.live for agent in agents))

    def test_positive_observation_does_not_resurrect_dead_agent(self) -> None:
        alice = SimpleNamespace(username="Alice", live=False)
        gate = EntityLivenessGate([alice])

        self.assertFalse(
            gate.update_from_observation("Alice", {"self": {"health": 20}})
        )
        self.assertFalse(alice.live)


if __name__ == "__main__":
    unittest.main()
