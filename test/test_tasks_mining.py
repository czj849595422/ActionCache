#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态矿物任务：生成、转换、运行时刷新与完成判定测试。"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    EvaluatorContext,
    MiningManager,
    TaskSpec,
    TaskStatus,
    create_default_registry,
    load_spec,
)
from src.tasks.mining import (  # noqa: E402
    MiningScheduleError,
    OreSpawnRecord,
    command_file_text,
    generate_spawn_schedule,
    generate_task_batch,
    generation_spec_from_template,
    load_generation_template,
    load_spawn_schedule,
    validate_spawn_schedule,
)

TEMPLATE_PATH = ROOT / "src" / "tasks" / "templates" / "mining.quick.yaml"
SPEC_PATH = ROOT / "src" / "tasks" / "specs" / "mining.yaml"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeServer:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        empty_response_for_missing: bool = False,
    ) -> None:
        self.commands: list[str] = []
        self.fail_on = fail_on
        self.empty_response_for_missing = empty_response_for_missing
        self.probe_names: set[str] = set()
        self.missing_positions: set[tuple[int, int, int]] = set()
        self.is_running = True

    def send_command(self, command: str, response_timeout: float = 5.0) -> str:
        if self.fail_on is not None and self.fail_on in command:
            raise RuntimeError("RCON failed")
        self.commands.append(command)
        if command.startswith("execute if block ") and " run data get block " in command:
            parts = command.split()
            position = (int(parts[3]), int(parts[4]), int(parts[5]))
            if position in self.missing_positions:
                return "" if self.empty_response_for_missing else "Test failed"
            return "The block is present"
        if command == "scoreboard players reset * mining_probe":
            self.probe_names.clear()
            return "ok"
        if command.startswith("execute store success score #mining_probe_"):
            match = re.search(
                r"score (#[^ ]+) mining_probe if block (-?\d+) (-?\d+) (-?\d+)",
                command,
            )
            if match is None:
                raise AssertionError(f"missing probe position: {command}")
            name = match.group(1)
            position = tuple(int(value) for value in match.groups()[1:])
            if position not in self.missing_positions:
                self.probe_names.add(name)
            return "ok"
        if command == "scoreboard players list":
            return "There are tracked entities: " + " ".join(sorted(self.probe_names))
        return "ok"

    def send_commands(self, commands, response_timeout: float = 5.0) -> list[str]:
        return [self.send_command(command, response_timeout) for command in commands]


class FakeAgent:
    def __init__(
        self,
        username: str,
        *,
        position: dict[str, float] | None = None,
        inventory: dict[str, int] | None = None,
    ) -> None:
        self.username = username
        self.position = position or {"x": 0.0, "y": 0.0, "z": 0.0}
        self.inventory = inventory or {}

    def get_raw_perception(self) -> dict:
        return {
            "self": {
                "health": 20,
                "position": dict(self.position),
                "inventory": dict(self.inventory),
            }
        }


def make_agents() -> list[FakeAgent]:
    return [
        FakeAgent("Alice", position={"x": -3, "y": 0, "z": -3}),
        FakeAgent("Bob", position={"x": 3, "y": 0, "z": -3}),
        FakeAgent("Carol", position={"x": -3, "y": 0, "z": 3}),
        FakeAgent("Dave", position={"x": 3, "y": 0, "z": 3}),
    ]


def make_context(
    server: FakeServer,
    *,
    elapsed: float,
    inventories: dict[str, dict[str, int]],
) -> EvaluatorContext:
    def perception(name: str):
        return {"self": {"health": 20, "inventory": inventories.get(name, {})}}

    return EvaluatorContext(
        server=server,
        perception=perception,
        elapsed_seconds=lambda: elapsed,
    )


def spec_with_seed(seed: int) -> TaskSpec:
    data = deepcopy(load_spec(SPEC_PATH).to_dict())
    data["params"]["generation_seed"] = seed
    data["metadata"]["generation_seed"] = seed
    return TaskSpec.from_dict(data)


def group_signature(manager: MiningManager) -> tuple:
    return tuple(
        (
            group.block,
            group.positions,
            group.spawned_at,
            group.expires_at,
        )
        for group in manager._active_groups
    )


class MiningGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        template = load_generation_template(TEMPLATE_PATH)
        self.generation_spec = generation_spec_from_template(template)

    def test_seed_is_reproducible_and_different_seed_changes_schedule(self) -> None:
        first = generate_spawn_schedule(self.generation_spec, 20260809)
        repeated = generate_spawn_schedule(self.generation_spec, 20260809)
        different = generate_spawn_schedule(self.generation_spec, 20260810)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)

    def test_generated_groups_are_valid_and_concurrent_distance_is_ten(self) -> None:
        records = generate_spawn_schedule(self.generation_spec, 20260809)
        self.assertEqual({record.y for record in records}, {0})
        groups = validate_spawn_schedule(
            records,
            bounds=self.generation_spec.bounds,
            min_anchor_distance=10,
            allowed_blocks={ore.block for ore in self.generation_spec.ores},
            timeout_seconds=self.generation_spec.timeout_seconds,
        )
        expected_groups = (
            (
                self.generation_spec.timeout_seconds
                - self.generation_spec.duration_max_seconds
            )
            // self.generation_spec.wave_interval_seconds
            + 1
        ) * self.generation_spec.groups_per_wave
        self.assertEqual(len(groups), expected_groups)
        self.assertTrue(all(len(group.positions) in (3, 4) for group in groups))
        for index, left in enumerate(groups):
            for right in groups[index + 1 :]:
                overlap = max(left.spawn_time_s, right.spawn_time_s) < min(
                    left.despawn_time_s,
                    right.despawn_time_s,
                )
                if overlap:
                    self.assertGreaterEqual(
                        math.hypot(
                            left.anchor[0] - right.anchor[0],
                            left.anchor[2] - right.anchor[2],
                        ),
                        10,
                    )

    def test_quick_preset_ores_values_and_weights(self) -> None:
        actual = {
            ore.item: (ore.value, ore.weight) for ore in self.generation_spec.ores
        }
        self.assertEqual(
            actual,
            {
                "lapis_block": (1.0, 30.0),
                "iron_block": (3.0, 25.0),
                "gold_block": (6.0, 18.0),
                "emerald_block": (8.0, 12.0),
                "diamond_block": (10.0, 10.0),
                "netherite_block": (20.0, 5.0),
            },
        )

    def test_batch_generation_writes_loadable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundles = generate_task_batch(
                TEMPLATE_PATH,
                count=2,
                base_seed=100,
                output_dir=directory,
            )
            self.assertEqual(len(bundles), 2)
            for bundle in bundles:
                spec = load_spec(bundle.task)
                manager = MiningManager(start_runtime_thread=False)
                manager.configure(spec)
                self.assertEqual(spec.completion["timeout_seconds"], 180)
                self.assertTrue(bundle.schedule.is_file())
                self.assertTrue(bundle.commands.is_file())

    def test_disconnected_group_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.txt"
            path.write_text(
                "g1 0 1 0 minecraft:iron_block 0 30\n"
                "g1 1 1 0 minecraft:iron_block 0 30\n"
                "g1 5 1 0 minecraft:iron_block 0 30\n",
                encoding="utf-8",
            )
            with self.assertRaises(MiningScheduleError):
                load_spawn_schedule(path)


class MiningConversionTests(unittest.TestCase):
    def test_equal_time_despawn_precedes_spawn(self) -> None:
        records = [
            OreSpawnRecord("g1", 0, 1, 0, "minecraft:iron_block", 0, 15),
            OreSpawnRecord("g2", 10, 1, 0, "minecraft:lapis_block", 15, 30),
        ]
        lines = [
            line
            for line in command_file_text(records).splitlines()
            if line.startswith("15s")
        ]
        self.assertIn("minecraft:iron_block run setblock", lines[0])
        self.assertIn("air run setblock", lines[1])

    def test_default_spec_no_longer_depends_on_static_schedule_files(self) -> None:
        spec = load_spec(SPEC_PATH)
        self.assertNotIn("schedule_file", spec.params)
        self.assertNotIn("command_file", spec.params)
        self.assertNotIn("min_anchor_distance", spec.params)
        self.assertEqual(spec.completion["timeout_seconds"], 180)
        manager = MiningManager(start_runtime_thread=False)
        manager.configure(spec)
        self.assertEqual(manager._generation_seed, 20260809)


class MiningManagerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        registry = create_default_registry()
        self.spec = registry.get("mining")
        self.manager = MiningManager(
            monotonic=self.clock.monotonic,
            start_runtime_thread=False,
        )
        self.manager.configure(self.spec)
        self.server = FakeServer()
        self.agents = make_agents()

    def tearDown(self) -> None:
        self.manager.teardown()

    def _prepare_and_start(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        self.manager.start()

    def test_prepare_gives_stone_iron_iron_diamond_pickaxes(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        commands = self.server.commands
        self.assertIn("setworldspawn 0 0 0", commands)
        self.assertIn("fill -25 0 -25 24 2 24 air replace", commands)
        self.assertIn("tp Alice -3 0 -3", commands)
        self.assertIn(
            "give Alice minecraft:stone_pickaxe{Unbreakable:1b} 1",
            commands,
        )
        self.assertIn(
            "give Bob minecraft:iron_pickaxe{Unbreakable:1b} 1",
            commands,
        )
        self.assertIn(
            "give Carol minecraft:iron_pickaxe{Unbreakable:1b} 1",
            commands,
        )
        self.assertIn(
            "give Dave minecraft:diamond_pickaxe{Unbreakable:1b} 1",
            commands,
        )

    def test_seed_controls_runtime_initial_groups(self) -> None:
        first = runtime_signature_for_seed(20260809)
        repeated = runtime_signature_for_seed(20260809)
        different = runtime_signature_for_seed(20260810)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)

    def test_start_places_eight_groups_with_runtime_constraints(self) -> None:
        self._prepare_and_start()
        groups = list(self.manager._active_groups)
        self.assertEqual(len(groups), 8)
        self.assertTrue(all(10 <= group.duration_s <= 20 for group in groups))
        positions = [position for group in groups for position in group.positions]
        self.assertEqual(len(positions), len(set(positions)))
        for x, _y, z in positions:
            for agent in self.agents:
                self.assertGreaterEqual(
                    math.hypot(x - agent.position["x"], z - agent.position["z"]),
                    5,
                )

    def test_runtime_attempts_one_spawn_every_three_seconds(self) -> None:
        self._prepare_and_start()
        self.clock.advance(2.9)
        self.manager._run_runtime_step(self.clock.monotonic())
        self.assertEqual(len(self.manager._active_groups), 8)

        self.clock.advance(0.1)
        self.manager._run_runtime_step(self.clock.monotonic())
        self.assertEqual(len(self.manager._active_groups), 9)
        self.assertEqual(self.manager.metrics(make_context(self.server, elapsed=3, inventories={}))["spawn_attempts"], 1)

    def test_runtime_spawns_two_groups_when_fewer_than_six_are_active(self) -> None:
        self._prepare_and_start()
        self.manager._active_groups = self.manager._active_groups[:5]

        self.manager._run_runtime_step(3.0)

        self.assertEqual(len(self.manager._active_groups), 7)
        self.assertEqual(
            self.manager.metrics(make_context(self.server, elapsed=3, inventories={}))["spawn_attempts"],
            1,
        )

    def test_runtime_spawns_one_group_when_six_are_active(self) -> None:
        self._prepare_and_start()
        self.manager._active_groups = self.manager._active_groups[:6]

        self.manager._run_runtime_step(3.0)

        self.assertEqual(len(self.manager._active_groups), 7)

    def test_runtime_skips_spawn_when_twelve_groups_are_active(self) -> None:
        self._prepare_and_start()
        while len(self.manager._active_groups) < 12:
            self.assertTrue(self.manager._spawn_next_group(self.clock.monotonic()))

        self.manager._run_runtime_step(3.0)
        metrics = self.manager.metrics(make_context(self.server, elapsed=3, inventories={}))
        self.assertEqual(metrics["active_groups"], 12)
        self.assertEqual(metrics["groups_skipped_capacity"], 1)

    def test_groups_are_removed_when_all_positions_are_mined(self) -> None:
        self._prepare_and_start()
        mined_group = self.manager._active_groups[0]
        self.server.missing_positions.update(mined_group.positions)
        self.manager._next_spawn_due = 999.0
        before = len(self.manager._active_groups)

        self.manager._run_runtime_step(100.0)

        self.assertLess(len(self.manager._active_groups), before)
        self.assertEqual(self.manager.metrics(make_context(self.server, elapsed=1, inventories={}))[
            "groups_mined"
        ], 1)
        self.assertNotIn(mined_group.group_id, {
            group.group_id for group in self.manager._active_groups
        })

    def test_presence_probe_does_not_retain_previous_success(self) -> None:
        self._prepare_and_start()
        mined_group = self.manager._active_groups[0]
        self.manager._run_runtime_step(1.0)
        self.server.missing_positions.update(mined_group.positions)
        self.manager._run_runtime_step(2.0)

        self.assertNotIn(
            mined_group.group_id,
            {group.group_id for group in self.manager._active_groups},
        )

    def test_live_groups_are_removed_at_expiry_time(self) -> None:
        self._prepare_and_start()
        self.manager._next_spawn_due = 999.0
        groups = list(self.manager._active_groups)
        expected_clear_count = sum(len(group.positions) for group in groups)
        expiry = max(group.expires_at for group in self.manager._active_groups)

        self.manager._run_runtime_step(expiry + 1.0)

        self.assertEqual(len(self.manager._active_groups), 0)
        self.assertEqual(
            self.manager.metrics(make_context(self.server, elapsed=1, inventories={}))["groups_expired"],
            len(groups),
        )
        self.assertEqual(
            sum(
                "run setblock" in command and command.endswith(" air")
                for command in self.server.commands
            ),
            expected_clear_count,
        )

    def test_expired_groups_free_capacity_for_new_spawns(self) -> None:
        self._prepare_and_start()
        self.manager._next_spawn_due = 999.0
        expiry = max(group.expires_at for group in self.manager._active_groups)

        self.manager._run_runtime_step(expiry + 1.0)
        self.manager._next_spawn_due = expiry + 1.0
        self.manager._run_runtime_step(expiry + 2.0)

        self.assertEqual(len(self.manager._active_groups), 2)
        self.assertEqual(
            self.manager.metrics(make_context(self.server, elapsed=1, inventories={}))["groups_expired"],
            8,
        )

    def test_partial_mining_keeps_one_manager_group_and_all_positions(self) -> None:
        self._prepare_and_start()
        group = self.manager._active_groups[0]
        missing = group.positions[0]
        self.server.missing_positions.add(missing)
        self.manager._next_spawn_due = 999.0

        self.manager._run_runtime_step(1.0)

        refreshed = next(item for item in self.manager._active_groups if item.group_id == group.group_id)
        self.assertEqual(len(refreshed.positions), len(group.positions) - 1)
        self.assertEqual(refreshed.all_positions, group.positions)

    def test_spawn_capacity_uses_refreshed_actual_group_count(self) -> None:
        self._prepare_and_start()
        missing_groups = self.manager._active_groups[:3]
        for group in missing_groups:
            self.server.missing_positions.update(group.positions)

        self.manager._run_runtime_step(3.0)

        self.assertEqual(len(self.manager._active_groups), 7)
        self.assertEqual(
            self.manager.metrics(make_context(self.server, elapsed=3, inventories={}))["groups_mined"],
            3,
        )

    def test_metrics_include_current_and_original_group_positions(self) -> None:
        self._prepare_and_start()
        group = self.manager._active_groups[0]
        self.server.missing_positions.add(group.positions[0])
        self.manager._run_runtime_step(1.0)

        metric_group = next(
            item for item in self.manager.metrics(make_context(self.server, elapsed=1, inventories={}))[
                "active_ore_groups"
            ] if item["group_id"] == group.group_id
        )
        self.assertEqual(len(metric_group["positions"]), len(group.positions) - 1)
        self.assertEqual(len(metric_group["all_positions"]), len(group.positions))

    def test_current_inventory_value_reaches_success_and_latches(self) -> None:
        self._prepare_and_start()
        inventories = {
            "Alice": {"lapis_block": 2},
            "Bob": {"gold_block": 3},
            "Carol": {"diamond_block": 2},
            "Dave": {"netherite_block": 3},
        }
        context = make_context(self.server, elapsed=10, inventories=inventories)
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.detail["current_value"], 100)
        self.assertEqual(len(self.manager._active_groups), 0)

        empty = make_context(self.server, elapsed=20, inventories={})
        self.assertIs(self.manager.completion_check(empty), outcome)

    def test_timeout_contains_value_metrics(self) -> None:
        self._prepare_and_start()
        timeout = float(self.spec.completion["timeout_seconds"])
        context = make_context(self.server, elapsed=timeout, inventories={})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.TIMEOUT)
        self.assertEqual(outcome.detail["current_value"], 0)
        self.assertEqual(len(self.manager._active_groups), 0)

    def test_runtime_command_error_causes_failure(self) -> None:
        manager = MiningManager(
            monotonic=self.clock.monotonic,
            start_runtime_thread=False,
        )
        manager.configure(self.spec)
        server = FakeServer(fail_on="setblock")
        manager.prepare(server, self.agents, memory=None)
        manager.start()
        context = make_context(server, elapsed=1, inventories={})
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.FAILURE)
        self.assertEqual(outcome.reason, "schedule_failed")
        self.assertTrue(outcome.detail["command_errors"])
        manager.teardown()

    def test_metrics_report_runtime_counts(self) -> None:
        self._prepare_and_start()
        context = make_context(self.server, elapsed=1, inventories={})
        metrics = self.manager.metrics(context)
        self.assertEqual(metrics["active_groups"], 8)
        self.assertEqual(metrics["groups_spawned"], 8)
        self.assertEqual(metrics["target_value"], 100)


def runtime_signature_for_seed(seed: int) -> tuple:
    clock = FakeClock()
    manager = MiningManager(
        monotonic=clock.monotonic,
        start_runtime_thread=False,
    )
    server = FakeServer()
    manager.configure(spec_with_seed(seed))
    manager.prepare(server, make_agents(), memory=None)
    manager.start()
    signature = group_signature(manager)
    manager.teardown()
    return signature


if __name__ == "__main__":
    unittest.main()
