#!/usr/bin/env python3
"""PVP task scheduling and manager tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.managers.pvp import PVPManager, pvp_team_rosters  # noqa: E402
from src.baseline.AutoGen import AutoGenConfig, AutoGenRunner  # noqa: E402
from src.memory import TeamMemory  # noqa: E402
from src.recording import RecordingConfig, RecordingManager  # noqa: E402
from src.tasks import TaskOutcome, TaskStatus, expand_task_args  # noqa: E402
from src.tasks.pvp import (  # noqa: E402
    PVPDropConfig,
    PVPDropRecord,
    PVPMatchResult,
    PVPSequentialRunner,
    PVP_SUPPLY_ITEM_NAMES,
    PVPTeamRunnerResult,
    class_probabilities,
    gear_probability,
    generate_drop_batch,
    generate_drop_positions,
    generate_drop_record,
    pvp_matchup_id,
    pvp_method_config,
    pvp_method_names,
    pvp_team_episode_ids,
    summon_drop_command,
)
from src.tasks.spec import TaskSpec, TaskSpecError, load_spec  # noqa: E402
import src.tasks.pvp.runner as pvp_runner_module  # noqa: E402
from src.tasks.pvp.runner import (  # noqa: E402
    PVPMatchRunner,
    PVPTeamRunConfig,
    pvp_spec_with_team_size,
)
import script.demo_pvp as demo_pvp  # noqa: E402


SPEC_PATH = ROOT / "src" / "tasks" / "specs" / "pvp.yaml"


class FakeAgent:
    def __init__(self, username: str) -> None:
        self.username = username


class FakeServer:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.scores: dict[str, int] = {}
        self.positions: dict[str, tuple[float, float, float]] = {}
        self.fail_gamemode = False

    def send_command(self, command: str) -> str:
        if self.fail_gamemode and command.startswith("gamemode spectator "):
            raise RuntimeError("RCON failed")
        self.commands.append(command)
        prefix = "scoreboard players get "
        if command.startswith(prefix):
            _, _, rest = command.partition(prefix)
            holder = rest.split()[0]
            return f"{holder} has {self.scores.get(holder, 0)}"
        data_prefix = "data get entity "
        if command.startswith(data_prefix) and command.endswith(" Pos"):
            rest = command[len(data_prefix):]
            holder = rest[:-4].strip()
            if holder not in self.positions:
                return "No entity was found"
            x, y, z = self.positions[holder]
            return f"{holder} has the following entity data: [{x}d, {y}d, {z}d]"
        return "ok"


class FakeContext:
    def __init__(
        self,
        elapsed: float,
        health: dict[str, float],
        snapshots: dict[str, dict] | None = None,
    ) -> None:
        self.elapsed = elapsed
        self.health_map = health
        self.snapshots = snapshots or {}

    def elapsed_seconds(self) -> float:
        return self.elapsed

    def health(self, agent_id: str) -> float | None:
        if agent_id in self.health_map:
            return self.health_map[agent_id]
        snapshot = self._snapshot(agent_id)
        if not snapshot:
            return None
        value = snapshot.get("self", {}).get("health")
        return None if value is None else float(value)

    def inventory(self, agent_id: str) -> dict[str, int] | None:
        snapshot = self._snapshot(agent_id)
        if not snapshot:
            return None
        inventory = snapshot.get("self", {}).get("inventory")
        if isinstance(inventory, dict):
            return dict(inventory)
        if isinstance(inventory, list):
            result: dict[str, int] = {}
            for item in inventory:
                name = item.get("name") if isinstance(item, dict) else None
                count = item.get("count") if isinstance(item, dict) else None
                if name and isinstance(count, int):
                    result[str(name)] = result.get(str(name), 0) + count
            return result
        return None

    def _snapshot(self, agent_id: str) -> dict | None:
        return self.snapshots.get(agent_id)


class FakeMemory:
    def __init__(self) -> None:
        self.chats: list[tuple[str, str, str, dict]] = []

    def append_history_event(
        self,
        *,
        agent_id,
        source,
        event_type,
        content,
        record_id=None,
        metadata=None,
    ):
        self.chats.append(
            (
                agent_id,
                content.get("message", ""),
                source,
                metadata or {},
            )
        )


class FakePVPSession:
    def __init__(self, spec: TaskSpec, agents: list[FakeAgent]) -> None:
        self.spec = spec
        self.agents = agents
        self.manager = SimpleNamespace()
        self.server = SimpleNamespace()
        self.world_workspace = None
        self.keep_server = True
        self.prepare_seconds = 0.0
        self.output = lambda _text: None
        self.memory = FakeMemory()
        self.context = object()

    def start(self):
        return self.context

    def close(self):
        return []


class PVPScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = pvp_spec_with_team_size(load_spec(SPEC_PATH), 2)
        self.config = PVPDropConfig.from_params(self.spec.params)

    def test_drop_probabilities_decay_and_disable_food_healing_at_300s(self) -> None:
        self.assertEqual(self.config.drops_per_wave, 5)
        self.assertEqual(self.config.min_drop_spacing, 20)
        self.assertAlmostEqual(gear_probability(0, self.config), 0.8)
        self.assertAlmostEqual(gear_probability(150, self.config), 0.5)
        self.assertAlmostEqual(gear_probability(300, self.config), 0.2)
        early = class_probabilities(0, self.config)
        self.assertAlmostEqual(early["gear"], 0.8)
        self.assertGreater(early["ammo"], early["food"])
        self.assertGreater(early["food"], early["healing"])
        self.assertIn("food", early)
        self.assertIn("healing", early)
        self.assertEqual(class_probabilities(300, self.config), {"ammo": 1.0})
        self.assertEqual(class_probabilities(301, self.config), {"ammo": 1.0})

    def test_drop_positions_are_spread_across_one_wave(self) -> None:
        positions = generate_drop_positions(
            config=self.config,
            rng=random.Random(11),
        )
        self.assertEqual(len(positions), 5)
        for x, y, z in positions:
            self.assertGreaterEqual(x, self.config.bounds.min_x)
            self.assertLessEqual(x, self.config.bounds.max_x)
            self.assertEqual(y, self.config.bounds.min_y)
            self.assertGreaterEqual(z, self.config.bounds.min_z)
            self.assertLessEqual(z, self.config.bounds.max_z)
        self.assertGreaterEqual(_min_distance_sq(positions), 20 * 20)

    def test_drop_batch_generates_five_records_with_sequential_ids(self) -> None:
        records = generate_drop_batch(
            config=self.config,
            rng=random.Random(12),
            spawn_index_start=5,
            elapsed_seconds=0,
        )
        self.assertEqual(len(records), 5)
        self.assertEqual(
            [record.spawn_id for record in records],
            [f"i{index:06d}" for index in range(5, 10)],
        )
        self.assertGreaterEqual(
            _min_distance_sq([record.position for record in records]),
            20 * 20,
        )

    def test_potion_drop_uses_drinkable_potion_not_splash(self) -> None:
        record = PVPDropRecord(
            spawn_id="i000001",
            x=1,
            y=0,
            z=2,
            resource="minecraft:potion",
            amount=1,
            profile="healing",
            item_class="healing",
            elapsed_seconds=10.0,
        )
        command = summon_drop_command(record, supply_tag="ac_pvp_sup")
        self.assertIn('id:"minecraft:potion"', command)
        self.assertIn('Potion:"minecraft:healing"', command)
        self.assertNotIn("splash_potion", command)

    def test_arrow_drop_is_group_of_eight(self) -> None:
        record = generate_drop_record(
            config=self.config,
            rng=random.Random(4),
            spawn_index=1,
            elapsed_seconds=301,
        )
        self.assertEqual(record.resource, "minecraft:arrow")
        self.assertEqual(record.amount, 8)
        self.assertIn("Count:8b", summon_drop_command(record, supply_tag="tag"))

    def test_gear_generation_uses_only_low_tier_weapons_and_leather_armor(self) -> None:
        data = self.spec.to_dict()
        data["params"]["gear_probability_start"] = 1.0
        data["params"]["gear_probability_end"] = 1.0
        config = PVPDropConfig.from_params(data["params"])
        resources = {
            generate_drop_record(
                config=config,
                rng=random.Random(seed),
                spawn_index=seed + 1,
                elapsed_seconds=0,
            ).resource
            for seed in range(200)
        }
        allowed_gear = {
            "minecraft:wooden_sword",
            "minecraft:stone_sword",
            "minecraft:iron_sword",
            "minecraft:bow",
            "minecraft:leather_helmet",
            "minecraft:leather_chestplate",
            "minecraft:leather_leggings",
            "minecraft:leather_boots",
        }
        self.assertLessEqual(resources, allowed_gear)
        self.assertFalse(any(resource.endswith("_axe") for resource in resources))
        self.assertTrue(any(resource.endswith("_sword") for resource in resources))
        self.assertIn("minecraft:bow", resources)
        self.assertEqual(
            {
                item
                for item in PVP_SUPPLY_ITEM_NAMES
                if item.endswith("_helmet")
                or item.endswith("_chestplate")
                or item.endswith("_leggings")
                or item.endswith("_boots")
            },
            {
                "leather_helmet",
                "leather_chestplate",
                "leather_leggings",
                "leather_boots",
            },
        )
        self.assertNotIn("golden_apple", PVP_SUPPLY_ITEM_NAMES)


class PVPMatchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = pvp_spec_with_team_size(load_spec(SPEC_PATH), 2)
        self.agents = [FakeAgent(name) for name in self.spec.agents["names"]]
        self.teams = (
            PVPTeamRunConfig("alpha", "basic_async", llm_client=object()),
            PVPTeamRunConfig("bravo", "direct_action", llm_client=object()),
        )

    def test_team_runners_enter_start_concurrently(self) -> None:
        starts: list[float] = []
        starts_lock = threading.Lock()
        runner_barrier = threading.Barrier(2)

        class BlockingRunner:
            def start(self) -> None:
                with starts_lock:
                    starts.append(time.monotonic())
                runner_barrier.wait(timeout=1.0)

        match = PVPMatchRunner(
            session=FakePVPSession(self.spec, self.agents),
            teams=self.teams,
            output=lambda _text: None,
        )
        match._team_runners = [BlockingRunner(), BlockingRunner()]

        match._start_team_runners()

        self.assertEqual(len(starts), 2)
        self.assertLess(max(starts) - min(starts), 0.2)

    def test_basic_and_direct_runners_receive_shared_planning_gate(self) -> None:
        created = []

        class GateCaptureRunner:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                created.append(self)

        original_factories = dict(pvp_runner_module._RUNNER_FACTORIES)
        try:
            pvp_runner_module._RUNNER_FACTORIES["basic_async"] = GateCaptureRunner
            pvp_runner_module._RUNNER_FACTORIES["direct_action"] = GateCaptureRunner
            match = PVPMatchRunner(
                session=FakePVPSession(self.spec, self.agents),
                teams=self.teams,
                output=lambda _text: None,
            )

            match._build_team_runners()
        finally:
            pvp_runner_module._RUNNER_FACTORIES.clear()
            pvp_runner_module._RUNNER_FACTORIES.update(original_factories)

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].kwargs["memory"].default_chat_window, 20)
        self.assertEqual(created[1].kwargs["memory"].default_chat_window, 20)
        gate_a = created[0].kwargs["planning_worker_kwargs"]["planning_start_gate"]
        gate_b = created[1].kwargs["planning_worker_kwargs"]["planning_start_gate"]
        released: list[str] = []

        thread = threading.Thread(
            target=lambda: (gate_a("Aria"), released.append("alpha")),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        self.assertEqual(released, [])

        gate_b("Eli")
        thread.join(timeout=1.0)
        self.assertEqual(released, ["alpha"])

    def test_autogen_is_registered_and_uses_pvp_broadcast_config(self) -> None:
        self.assertIn("autogen", pvp_method_names())
        self.assertIsInstance(pvp_method_config("auto-gen"), AutoGenConfig)
        config = pvp_method_config("autogen", broadcast_results=False)
        self.assertFalse(config.broadcast_results)
        self.assertFalse(config.minecraft_say_results)

    def test_autogen_team_runners_are_isolated_and_use_planning_gate(self) -> None:
        teams = (
            PVPTeamRunConfig("alpha", "autogen", llm_client=object()),
            PVPTeamRunConfig("bravo", "autogen", llm_client=object()),
        )
        match = PVPMatchRunner(
            session=FakePVPSession(self.spec, self.agents),
            teams=teams,
            output=lambda _text: None,
        )
        match._build_team_runners()

        self.assertEqual(len(match._team_runners), 2)
        self.assertTrue(
            all(isinstance(runner, AutoGenRunner) for runner in match._team_runners)
        )
        self.assertEqual(
            [agent.username for agent in match._team_runners[0].session.agents],
            ["Aria", "Blake"],
        )
        self.assertEqual(
            [agent.username for agent in match._team_runners[1].session.agents],
            ["Eli", "Faye"],
        )
        self.assertIsNot(match._team_runners[0].memory, match._team_runners[1].memory)
        self.assertIsNot(
            match._team_runners[0].llm_client,
            teams[0].llm_client,
        )
        self.assertIsNot(
            match._team_runners[1].llm_client,
            teams[1].llm_client,
        )

    def test_llm_client_gate_blocks_first_request_until_both_teams_arrive(self) -> None:
        released: list[str] = []
        gate = pvp_runner_module._PVPPlanningStartGate(
            ["alpha", "bravo"],
            on_release=lambda: released.append("started"),
        )
        calls: list[str] = []

        class Delegate:
            def __init__(self, label: str) -> None:
                self.label = label

            def complete(self, request) -> str:
                calls.append(self.label)
                return self.label

        alpha = pvp_runner_module._PVPPlanningGateLLMClient(
            Delegate("alpha"),
            gate=gate,
            team_id="alpha",
        )
        bravo = pvp_runner_module._PVPPlanningGateLLMClient(
            Delegate("bravo"),
            gate=gate,
            team_id="bravo",
        )
        returned: list[str] = []
        thread = threading.Thread(
            target=lambda: returned.append(alpha.complete("request")),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        self.assertEqual(calls, [])

        self.assertEqual(bravo.complete("request"), "bravo")
        thread.join(timeout=1.0)
        self.assertEqual(sorted(calls), ["alpha", "bravo"])
        self.assertEqual(returned, ["alpha"])
        self.assertEqual(released, ["started"])

    def test_match_timer_starts_when_both_teams_reach_planning_gate(self) -> None:
        now = [100.0]
        match = PVPMatchRunner(
            session=FakePVPSession(self.spec, self.agents),
            teams=self.teams,
            max_run_seconds=60.0,
            wall_time=lambda: now[0],
            output=lambda _text: None,
        )
        self.assertIsNone(match._result.started_at)

        waiting = threading.Thread(
            target=lambda: match._planning_start_gate.wait("alpha"),
            daemon=True,
        )
        waiting.start()
        time.sleep(0.05)
        self.assertIsNone(match._result.started_at)

        now[0] = 250.0
        match._planning_start_gate.wait("bravo")
        waiting.join(timeout=1.0)

        self.assertEqual(match._result.started_at, 250.0)

    def test_system_broadcast_memory_fans_out_to_both_team_memories(self) -> None:
        team_a_memory = TeamMemory()
        team_b_memory = TeamMemory()
        match = PVPMatchRunner(
            session=FakePVPSession(self.spec, self.agents),
            teams=(
                PVPTeamRunConfig(
                    "alpha",
                    "basic_async",
                    llm_client=object(),
                    memory=team_a_memory,
                ),
                PVPTeamRunConfig(
                    "bravo",
                    "direct_action",
                    llm_client=object(),
                    memory=team_b_memory,
                ),
            ),
            output=lambda _text: None,
        )

        match._ensure_team_memories()
        match.session.memory.append_history_event(
            agent_id="system",
            source="system",
            event_type="system",
            content={"message": "Aria（1, 0, 2）死亡。"},
            metadata={"source": "task_manager.pvp_death", "agent": "Aria"},
        )

        for memory, agent_id in (
            (team_a_memory, "Aria"),
            (team_b_memory, "Eli"),
        ):
            self.assertTrue(
                any(
                    item["source"] == "system"
                    and item["content"].get("message") == "Aria（1, 0, 2）死亡。"
                    for item in memory.to_prompt_context(agent_id, chat_window=5)[
                        "history"
                    ]
                )
            )


class PVPSequentialRunnerTests(unittest.TestCase):
    def test_runs_numbered_episodes_with_fresh_clients_and_pvp_metrics(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            base = Path(tmp)
            recorder = RecordingManager(
                RecordingConfig(root_dir=base)
            ).start_run("PVP", task="basic_async_vs_direct_action")
            specs = expand_task_args(["pvp"], seed=0, episode_count=3)
            seen_seeds: list[int] = []
            closed_bundles: list[str] = []
            clients: list[SimpleNamespace] = []
            created_matches: list[SimpleNamespace] = []

            def bundle_factory(spec, manager, episode):
                seen_seeds.append(spec.params["generation_seed"])
                return SimpleNamespace(
                    spec=spec,
                    manager=manager,
                    server=SimpleNamespace(is_running=False),
                    agents=[FakeAgent(name) for name in spec.agents["names"]],
                    world_workspace=None,
                    registry_file=base / f"registry-{episode.context.episode_id}.json",
                    close_runtime=lambda: closed_bundles.append(
                        episode.context.episode_id
                    ),
                )

            def llm_client_factory(recorder):
                client = SimpleNamespace(
                    recorder=recorder,
                    closed=False,
                    close=lambda: setattr(client, "closed", True),
                )
                clients.append(client)
                return client

            class FakeMatchRunner:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs
                    created_matches.append(SimpleNamespace(**kwargs))

                def run(self):
                    teams = self.kwargs["teams"]
                    return PVPMatchResult(
                        outcome=TaskOutcome(
                            TaskStatus.SUCCESS,
                            reason="team_eliminated",
                            detail={"winner": "alpha"},
                        ),
                        final_metrics={"elapsed_seconds": 20.0},
                        cleanup_errors=[],
                        team_results=[
                            PVPTeamRunnerResult(
                                team_id=teams[0].team_id,
                                method=teams[0].method,
                            ),
                            PVPTeamRunnerResult(
                                team_id=teams[1].team_id,
                                method=teams[1].method,
                            ),
                        ],
                        started_at=100.0,
                        finished_at=120.0,
                    )

            sequential = PVPSequentialRunner(
                specs=specs,
                team_a_method="basic_async",
                team_b_method="direct_action",
                llm_client_factory=llm_client_factory,
                run_recorder=recorder,
                server_dir=base,
                bridge_port_start=3201,
                benchmark_root=base,
                team_size=2,
                bundle_factory=bundle_factory,
                match_runner_factory=FakeMatchRunner,
                start_episode=1,
                episode_count=3,
                output=lambda _text: None,
            )
            result = sequential.run()
            recorder.finalize(status="completed" if result.exit_code == 0 else "failed")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["1", "2", "3"],
            )
            self.assertEqual(seen_seeds, [0, 1, 2])
            self.assertEqual(closed_bundles, ["1", "2", "3"])
            self.assertEqual(len(created_matches), 3)
            self.assertEqual(len(clients), 6)
            self.assertEqual(len({id(client) for client in clients}), 6)
            self.assertTrue(all(client.closed for client in clients))
            for episode_id in ("1", "2", "3"):
                self.assertTrue((recorder.path / episode_id).is_dir())
                self.assertTrue(
                    (recorder.path / episode_id / "team-basic_async").is_dir()
                )
                self.assertTrue(
                    (recorder.path / episode_id / "team-direct_action").is_dir()
                )

            summary = json.loads(
                (recorder.path / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["pvp_metrics"]["match_count"], 3)
            self.assertEqual(summary["pvp_metrics"]["winner_count"], 3)
            self.assertEqual(summary["aggregate_metrics"]["episode_count"], 3)

    def test_start_episode_uses_seed_offset(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            base = Path(tmp)
            recorder = RecordingManager(
                RecordingConfig(root_dir=base)
            ).start_run("PVP", task="basic_async_vs_direct_action")
            for episode_id in ("1", "2", "3"):
                episode = recorder.start_episode(episode_id=episode_id)
                episode.finalize(
                    status="completed",
                    result={
                        "outcome": {
                            "status": "success",
                            "reason": "team_eliminated",
                            "detail": {"winner": "alpha"},
                        },
                        "team_results": [
                            {"team_id": "alpha", "method": "basic_async"},
                            {"team_id": "bravo", "method": "direct_action"},
                        ],
                    },
                )
            expanded = expand_task_args(
                ["pvp@0"],
                start_episode=4,
                episode_count=2,
            )
            selected = expanded[3:5]
            seen_seeds: list[int] = []

            def bundle_factory(spec, manager, episode):
                seen_seeds.append(spec.params["generation_seed"])
                return SimpleNamespace(
                    spec=spec,
                    manager=manager,
                    server=SimpleNamespace(is_running=False),
                    agents=[FakeAgent(name) for name in spec.agents["names"]],
                    world_workspace=None,
                    registry_file=base / f"registry-{episode.context.episode_id}.json",
                    close_runtime=lambda: None,
                )

            class FakeMatchRunner:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

                def run(self):
                    teams = self.kwargs["teams"]
                    return PVPMatchResult(
                        outcome=TaskOutcome(
                            TaskStatus.SUCCESS,
                            reason="team_eliminated",
                            detail={"winner": "bravo"},
                        ),
                        final_metrics={"elapsed_seconds": 10.0},
                        cleanup_errors=[],
                        team_results=[
                            PVPTeamRunnerResult(
                                team_id=teams[0].team_id,
                                method=teams[0].method,
                            ),
                            PVPTeamRunnerResult(
                                team_id=teams[1].team_id,
                                method=teams[1].method,
                            ),
                        ],
                    )

            result = PVPSequentialRunner(
                specs=selected,
                team_a_method="basic_async",
                team_b_method="direct_action",
                llm_client_factory=lambda _recorder: SimpleNamespace(close=lambda: None),
                run_recorder=recorder,
                server_dir=base,
                bridge_port_start=3201,
                benchmark_root=base,
                team_size=2,
                bundle_factory=bundle_factory,
                match_runner_factory=FakeMatchRunner,
                start_episode=4,
                episode_count=2,
                output=lambda _text: None,
            ).run()

            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["4", "5"],
            )
            self.assertEqual(seen_seeds, [3, 4])
            recorder.finalize()

    def test_episode_failure_is_recorded_and_next_episode_continues(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            base = Path(tmp)
            recorder = RecordingManager(
                RecordingConfig(root_dir=base)
            ).start_run("PVP", task="basic_async_vs_direct_action")
            specs = expand_task_args(["pvp@0"], episode_count=2)
            started_matches: list[str] = []

            def bundle_factory(spec, manager, episode):
                return SimpleNamespace(
                    spec=spec,
                    manager=manager,
                    server=SimpleNamespace(is_running=False),
                    agents=[FakeAgent(name) for name in spec.agents["names"]],
                    world_workspace=None,
                    registry_file=base / f"registry-{episode.context.episode_id}.json",
                    close_runtime=lambda: None,
                )

            class FakeMatchRunner:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

                def run(self):
                    episode_id = self.kwargs["episode_recorder"].context.episode_id
                    started_matches.append(episode_id)
                    if episode_id == "1":
                        raise RuntimeError("boom")
                    teams = self.kwargs["teams"]
                    return PVPMatchResult(
                        outcome=TaskOutcome(
                            TaskStatus.SUCCESS,
                            reason="team_eliminated",
                            detail={"winner": "alpha"},
                        ),
                        final_metrics={"elapsed_seconds": 12.0},
                        cleanup_errors=[],
                        team_results=[
                            PVPTeamRunnerResult(
                                team_id=teams[0].team_id,
                                method=teams[0].method,
                            ),
                            PVPTeamRunnerResult(
                                team_id=teams[1].team_id,
                                method=teams[1].method,
                            ),
                        ],
                    )

            result = PVPSequentialRunner(
                specs=specs,
                team_a_method="basic_async",
                team_b_method="direct_action",
                llm_client_factory=lambda _recorder: SimpleNamespace(close=lambda: None),
                run_recorder=recorder,
                server_dir=base,
                bridge_port_start=3201,
                benchmark_root=base,
                team_size=2,
                bundle_factory=bundle_factory,
                match_runner_factory=FakeMatchRunner,
                episode_count=2,
                output=lambda _text: None,
            ).run()
            recorder.finalize(status="failed")

            self.assertEqual(started_matches, ["1", "2"])
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["1", "2"],
            )
            summary = json.loads(
                (recorder.path / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                summary["episode_status_counts"],
                {"completed": 1, "failed": 1},
            )
            self.assertEqual(summary["pvp_metrics"]["match_count"], 2)

    def test_resume_start_episode_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            recorder = manager.start_run(
                "PVP",
                task="basic_async_vs_direct_action",
                run_id="resume-pvp",
            )
            for episode_id in ("1", "2"):
                episode = recorder.start_episode(episode_id=episode_id)
                episode.finalize(
                    status="completed",
                    result={
                        "outcome": {
                            "status": "success",
                            "reason": "team_eliminated",
                            "detail": {"winner": "alpha"},
                        },
                        "team_results": [
                            {"team_id": "alpha", "method": "basic_async"},
                            {"team_id": "bravo", "method": "direct_action"},
                        ],
                    },
                )
            recorder.finalize()
            reopened = manager.open_run(
                "PVP",
                task="basic_async_vs_direct_action",
                run_id="resume-pvp",
            )

            with self.assertRaisesRegex(ValueError, "已有 2 个 episode"):
                PVPSequentialRunner(
                    specs=expand_task_args(["pvp@3"]),
                    team_a_method="basic_async",
                    team_b_method="direct_action",
                    llm_client_factory=lambda _recorder: SimpleNamespace(
                        close=lambda: None
                    ),
                    run_recorder=reopened,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    start_episode=4,
                    output=lambda _text: None,
                ).run()

    def test_demo_check_default_multi_seed_starts_at_zero(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = demo_pvp.main(
                [
                    "--task",
                    "pvp",
                    "--episode-count",
                    "3",
                    "--team-size",
                    "2",
                    "--check",
                ]
            )

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("episodes=3", output)
        self.assertIn("episode_ids=[1, 2, 3]", output)
        self.assertIn("seeds=[0, 1, 2]", output)


class PVPManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = pvp_spec_with_team_size(load_spec(SPEC_PATH), 2)
        self.names = list(self.spec.agents["names"])
        self.agents = [FakeAgent(name) for name in self.names]

    def spec_with_params(self, **params) -> TaskSpec:
        data = self.spec.to_dict()
        data["params"].update(params)
        return TaskSpec.from_dict(data)

    def build_manager(
        self,
        spec: TaskSpec | None = None,
        memory=None,
        agents: list[FakeAgent] | None = None,
    ) -> tuple[PVPManager, FakeServer]:
        spec = spec or self.spec
        manager = PVPManager(supply_thread_enabled=False)
        manager.configure(spec)
        server = FakeServer()
        selected_agents = agents or [FakeAgent(name) for name in spec.agents["names"]]
        manager.prepare(server, selected_agents, memory=memory)
        manager.start()
        return manager, server

    def test_spec_rosters_are_two_symmetric_teams(self) -> None:
        alpha, bravo = pvp_team_rosters(self.spec)
        self.assertEqual(alpha.team_id, "alpha")
        self.assertEqual(bravo.team_id, "bravo")
        self.assertEqual(alpha.agents, ("Aria", "Blake"))
        self.assertEqual(bravo.agents, ("Eli", "Faye"))

    def test_initial_loadout_is_fixed_to_wooden_sword_and_bow(self) -> None:
        self.assertEqual(
            self.spec.initial_state["per_agent"]["inventory"],
            {
                "minecraft:wooden_sword": 1,
                "minecraft:bow": 1,
            },
        )

        data = self.spec.to_dict()
        data["initial_state"]["per_agent"]["inventory"]["minecraft:arrow"] = 8
        with self.assertRaises(TaskSpecError):
            PVPManager(supply_thread_enabled=False).configure(TaskSpec.from_dict(data))

        data = self.spec.to_dict()
        data["initial_state"]["by_agent"][0]["inventory"] = {"minecraft:arrow": 8}
        with self.assertRaises(TaskSpecError):
            PVPManager(supply_thread_enabled=False).configure(TaskSpec.from_dict(data))

    def test_team_episode_ids_use_method_names_and_disambiguate_duplicates(self) -> None:
        self.assertEqual(
            pvp_team_episode_ids("basic_async", "direct_action"),
            ("team-basic_async", "team-direct_action"),
        )
        self.assertEqual(
            pvp_team_episode_ids("basic", "basic_async"),
            ("team-basic_async-1", "team-basic_async-2"),
        )

    def test_matchup_id_uses_normalized_method_names(self) -> None:
        self.assertEqual(
            pvp_matchup_id("basic", "direct"),
            "basic_async_vs_direct_action",
        )

    def test_prepare_closes_friendly_fire_clears_agents_and_disables_mobs(self) -> None:
        _, server = self.build_manager()
        joined = "\n".join(server.commands)
        self.assertIn("team modify alpha friendlyFire false", joined)
        self.assertIn("team modify bravo friendlyFire false", joined)
        self.assertIn("gamerule doMobSpawning false", joined)
        self.assertIn("gamerule naturalRegeneration false", joined)
        self.assertIn("kill @e[type=!player", joined)
        self.assertIn("team leave @a[name=!Aria,name=!Blake,name=!Eli,name=!Faye]", joined)
        for name in self.names:
            self.assertIn(f"clear {name}", joined)
            self.assertIn(f"give {name} minecraft:wooden_sword 1", joined)
            self.assertIn(f"give {name} minecraft:bow 1", joined)
            self.assertIn(f"gamemode survival {name}", joined)

    def test_supply_wave_summons_five_spread_drops(self) -> None:
        manager, server = self.build_manager()
        before = len(server.commands)

        records, commands, responses = manager.spawn_supply_once(elapsed_seconds=0)
        metrics = manager.metrics(FakeContext(0, {name: 20 for name in self.names}))

        self.assertEqual(len(records), 5)
        self.assertEqual(len(commands), 5)
        self.assertEqual(len(responses), 5)
        self.assertEqual(server.commands[before:], commands)
        self.assertTrue(
            all(command.startswith("summon minecraft:item") for command in commands)
        )
        self.assertGreaterEqual(
            _min_distance_sq([record.position for record in records]),
            20 * 20,
        )
        self.assertEqual(metrics["spawned_supplies"], 5)
        self.assertEqual(metrics["spawned_supply_waves"], 1)
        self.assertEqual(len(metrics["last_drop_batch"]), 5)
        self.assertEqual(metrics["last_drop"], records[-1].to_dict())

    def test_team_eliminated_success_has_winner_and_ignores_timeout(self) -> None:
        manager, server = self.build_manager()
        server.scores.update({"Eli": 1, "Faye": 1})
        context = FakeContext(90000, {name: 20 for name in self.names})
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status.value, "success")
        self.assertEqual(outcome.reason, "team_eliminated")
        self.assertEqual(outcome.detail["winner"], "alpha")

    def test_prepare_filters_non_team_observer_from_task_agents(self) -> None:
        observer = FakeAgent("observer")
        manager, server = self.build_manager(agents=[*self.agents, observer])
        joined = "\n".join(server.commands)

        self.assertIn("team leave @a[name=!Aria,name=!Blake,name=!Eli,name=!Faye]", joined)
        self.assertNotIn("gamemode survival observer", joined)
        self.assertNotIn("clear observer", joined)
        self.assertFalse(hasattr(observer, "live"))
        self.assertTrue(all(getattr(agent, "live", False) for agent in self.agents))
        self.assertEqual(
            manager.metrics(FakeContext(0, {name: 20 for name in self.names}))[
                "alive_agents"
            ],
            self.names,
        )

    def test_periodic_rules_do_not_repeat_non_team_cleanup(self) -> None:
        manager, server = self.build_manager()
        before = len(server.commands)
        context = FakeContext(5, {name: 20 for name in self.names})

        outcome = manager.completion_check(context)

        self.assertEqual(outcome.status.value, "running")
        self.assertFalse(
            any(command.startswith("team leave") for command in server.commands[before:])
        )

    def test_death_broadcast_is_published_once_with_position(self) -> None:
        memory = FakeMemory()
        manager, server = self.build_manager(memory=memory)
        server.scores["Aria"] = 1
        server.positions["Aria"] = (1.25, 0.0, -3.5)
        context = FakeContext(0, {name: 20 for name in self.names})

        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status.value, "running")
        message = "Aria（1.25, 0, -3.5）死亡。"
        self.assertIn(f"say {message}", server.commands)
        self.assertEqual(memory.chats, [
            (
                "system",
                message,
                "system",
                {
                    "source": "task_manager.pvp_death",
                    "agent": "Aria",
                    "detected_by": "completion_check",
                },
            )
        ])

        manager.completion_check(context)
        self.assertEqual(
            sum(command == f"say {message}" for command in server.commands),
            1,
        )

    def test_failed_spectator_command_is_reported_after_retries(self) -> None:
        manager, server = self.build_manager()
        server.fail_gamemode = True
        server.scores["Aria"] = 1
        context = FakeContext(0, {name: 20 for name in self.names})

        outcomes = [manager.completion_check(context) for _ in range(3)]

        self.assertEqual(outcomes[-1].status.value, "failure")
        self.assertEqual(outcomes[-1].reason, "agent_elimination_failed")
        self.assertNotIn("Aria", manager._eliminated_agents)

    def test_death_drops_inventory_and_equipment_for_pickup(self) -> None:
        manager, server = self.build_manager()
        server.scores["Aria"] = 1
        before = len(server.commands)
        snapshots = {
            "Aria": {
                "self": {
                    "health": 0,
                    "position": {"x": 1, "y": 0, "z": 2},
                    "inventory": [
                        {"name": "arrow", "count": 8, "slot": 9},
                        {
                            "name": "potion",
                            "count": 1,
                            "slot": 10,
                            "nbt": {"Potion": "minecraft:healing"},
                        },
                    ],
                    "equipmentSlots": {
                        "head": {"name": "iron_helmet", "count": 1},
                    },
                }
            }
        }
        context = FakeContext(0, {}, snapshots=snapshots)

        outcome = manager.completion_check(context)

        self.assertEqual(outcome.status.value, "running")
        new_commands = server.commands[before:]
        summons = [
            command
            for command in new_commands
            if command.startswith("summon minecraft:item")
        ]
        joined = "\n".join(summons)
        self.assertIn('summon minecraft:item 1 0 2', joined)
        self.assertIn('Item:{id:"minecraft:arrow",Count:8b}', joined)
        self.assertIn(
            'Item:{id:"minecraft:potion",Count:1b,tag:{Potion:"minecraft:healing"}}',
            joined,
        )
        self.assertIn('Item:{id:"minecraft:iron_helmet",Count:1b}', joined)
        self.assertIn("clear Aria", new_commands)
        spectator_command = (
            "gamemode spectator @a[name=Aria,scores={acPvpDeath=1..}]"
        )
        self.assertIn(spectator_command, new_commands)
        self.assertLess(
            new_commands.index(spectator_command),
            new_commands.index(summons[0]),
        )

    def test_death_monitor_spectates_immediately_and_drops_at_death_position(self) -> None:
        memory = FakeMemory()
        manager = PVPManager(
            supply_thread_enabled=False,
            death_monitor_enabled=True,
        )
        manager.configure(self.spec_with_params(death_monitor_interval_seconds=0.01))
        server = FakeServer()
        selected_agents = [FakeAgent(name) for name in self.names]
        manager.prepare(server, selected_agents, memory=memory)
        manager.start()
        try:
            server.positions["Aria"] = (1, 0, 2)
            server.scores["Aria"] = 1
            deadline = time.monotonic() + 1.0
            while (
                "gamemode spectator @a[name=Aria,scores={acPvpDeath=1..}]"
                not in server.commands
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertIn("scoreboard players set Aria acPvpAlive 0", server.commands)
            self.assertIn(
                "gamemode spectator @a[name=Aria,scores={acPvpDeath=1..}]",
                server.commands,
            )
            self.assertFalse(getattr(selected_agents[0], "live"))
            self.assertEqual(memory.chats[0][3]["detected_by"], "death_monitor")

            before = len(server.commands)
            context = FakeContext(
                0,
                {},
                snapshots={
                    "Aria": {
                        "self": {
                            "health": 20,
                            "position": {"x": 99, "y": 0, "z": 99},
                            "inventory": [
                                {"name": "arrow", "count": 8, "slot": 9},
                            ],
                        }
                    }
                },
            )

            outcome = manager.completion_check(context)

            self.assertEqual(outcome.status.value, "running")
            new_commands = server.commands[before:]
            summons = [
                command
                for command in new_commands
                if command.startswith("summon minecraft:item")
            ]
            self.assertEqual(len(summons), 1)
            self.assertIn("summon minecraft:item 1 0 2", summons[0])
            self.assertIn("clear Aria", new_commands)
            self.assertEqual(
                sum(command.startswith("summon minecraft:item") for command in server.commands),
                1,
            )
        finally:
            manager.teardown()

    def test_death_drops_can_be_disabled_for_pvp(self) -> None:
        spec = self.spec_with_params(death_drops_enabled=False)
        manager, server = self.build_manager(spec=spec)
        server.scores["Aria"] = 1
        before = len(server.commands)
        context = FakeContext(
            0,
            {},
            snapshots={
                "Aria": {
                    "self": {
                        "health": 0,
                        "position": {"x": 1, "y": 0, "z": 2},
                        "inventory": [{"name": "arrow", "count": 8, "slot": 9}],
                    }
                }
            },
        )

        manager.completion_check(context)

        new_commands = server.commands[before:]
        self.assertFalse(
            any(command.startswith("summon minecraft:item") for command in new_commands)
        )
        self.assertIn("clear Aria", new_commands)

    def test_periodic_out_of_bounds_and_hunger_commands_are_applied(self) -> None:
        manager, server = self.build_manager()
        context = FakeContext(310, {name: 20 for name in self.names})
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status.value, "running")
        joined = "\n".join(server.commands)
        self.assertIn("effect give Aria minecraft:wither 11 0 true", joined)
        self.assertIn("effect give Aria minecraft:hunger 10 3 true", joined)
        self.assertIn("effect give Aria minecraft:wither 11 0 true", server.commands)
        self.assertTrue(outcome.detail["food_healing_disabled"])


def _min_distance_sq(positions) -> float:
    values = list(positions)
    if len(values) < 2:
        return float("inf")
    return min(
        float((left[0] - right[0]) ** 2 + (left[2] - right[2]) ** 2)
        for index, left in enumerate(values)
        for right in values[index + 1 :]
    )


if __name__ == "__main__":
    unittest.main()
