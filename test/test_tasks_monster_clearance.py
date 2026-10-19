#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态怪物清剿任务：生成、转换、管理与完成判定测试。"""

from __future__ import annotations

import math
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.launcher.command_scheduler import BatchResult, CommandEntry  # noqa: E402
from src.tasks import (  # noqa: E402
    EvaluatorContext,
    MonsterClearanceManager,
    TaskSpec,
    TaskStatus,
    create_default_registry,
    expand_task_args,
    load_spec,
)
from src.tasks.monster_clearance import (  # noqa: E402
    MonsterClearanceScheduleError,
    command_file_text,
    generate_spawn_schedule,
    generate_task_batch,
    generation_spec_from_template,
    load_generation_template,
    load_spawn_schedule,
    validate_spawn_schedule,
)

TEMPLATE_PATH = (
    ROOT
    / "src"
    / "tasks"
    / "monster_clearance"
    / "template.yaml"
)
SPEC_PATH = (
    ROOT / "src" / "tasks" / "specs" / "monster_clearance.yaml"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeServer:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.commands: list[str] = []
        self.priority_commands: list[str] = []
        self.fail_on = fail_on
        self.is_running = True
        self.death_scores = {("Alice", "Bob", "Carol", "Dave", "Eve")[index]: 0 for index in range(4)}
        self.spawn_scores: dict[str, int] = {}
        self.monster_health: dict[str, float] = {}
        self.alive_scores: dict[str, int] = {}
        self.alive_query_errors: set[str] = set()
        self.agent_health = {("Alice", "Bob", "Carol", "Dave", "Eve")[index]: 20.0 for index in range(4)}
        self.agent_positions = {("Alice", "Bob", "Carol", "Dave", "Eve")[index]: (0.0, 0.0, 0.0) for index in range(4)}

    def send_priority_command(self, command: str, response_timeout: float = 5.0) -> str:
        self.priority_commands.append(command)
        return self.send_command(command, response_timeout=response_timeout)

    def send_command(self, command: str, response_timeout: float = 5.0) -> str:
        if self.fail_on is not None and self.fail_on in command:
            raise RuntimeError("RCON failed")
        self.commands.append(command)
        if command.startswith("scoreboard players set "):
            _, _, _, holder, objective, value = command.split()
            if objective == "acTaskDeath":
                self.death_scores[holder] = int(value)
            elif objective == "acAlive":
                self.alive_scores[holder] = int(value)
            else:
                self.spawn_scores[holder.lstrip("#")] = int(value)
            return "ok"
        if command.startswith("scoreboard players get "):
            _, _, _, holder, objective = command.split()
            if objective == "acTaskDeath":
                value = self.death_scores.get(holder, 0)
            elif objective == "acAlive":
                value = self.alive_scores.get(holder.lstrip("#"), 0)
            else:
                spawn_id = holder.lstrip("#")
                value = self.spawn_scores.get(spawn_id, 1)
            return f"{holder} has {value} [{objective}]"
        if command.startswith("execute store result score "):
            parts = command.split()
            holder = parts[4].lstrip("#")
            objective = parts[5]
            if objective == "acAlive":
                if holder in self.alive_query_errors:
                    raise RuntimeError("RCON failed")
                self.alive_scores[holder] = (
                    1 if holder in self.monster_health else 0
                )
                return "ok"
        if command.startswith("data get entity "):
            parts = command.split()
            if len(parts) >= 5:
                target = parts[3]
                field = parts[4]
                if field == "Health":
                    value = self.agent_health.get(target)
                    if value is None:
                        return "No entity was found"
                    return f"{target} has the following entity data: {value}f"
                if field == "Pos":
                    position = self.agent_positions.get(target)
                    if position is None:
                        return "No entity was found"
                    x, y, z = position
                    return (
                        f"{target} has the following entity data: "
                        f"[{x}d, {y}d, {z}d]"
                    )
        return "ok"


class FakeAgent:
    def __init__(
        self,
        username: str,
        *,
        position: dict[str, float] | None = None,
        health: float = 20.0,
        game_mode: str = "survival",
    ) -> None:
        self.username = username
        self.position = position or {"x": 0.0, "y": 0.0, "z": 0.0}
        self.health = health
        self.game_mode = game_mode

    def get_raw_perception(self) -> dict:
        return {
            "self": {
                "health": self.health,
                "position": dict(self.position),
                "gameMode": self.game_mode,
                "inventory": {},
            }
        }


class ImmediateScheduler:
    next_errors: list[tuple[CommandEntry, str]] = []
    sent_count: int | None = None

    def __init__(self, server, plan, poll_interval=0.1, **kwargs) -> None:
        self.server = server
        self.plan = plan
        self.cancelled = False
        count = len(plan.task) if type(self).sent_count is None else type(self).sent_count
        self.result = BatchResult(
            total=len(plan.task),
            sent=[(entry, "ok") for entry in plan.task[:count]],
            errors=list(type(self).next_errors),
            started=True,
        )

    def run_task(self) -> BatchResult:
        return self.result

    def cancel(self) -> None:
        self.cancelled = True
        self.result.cancelled = True

    def result_snapshot(self) -> BatchResult:
        return BatchResult(
            total=self.result.total,
            sent=list(self.result.sent),
            errors=list(self.result.errors),
            started=self.result.started,
            cancelled=self.result.cancelled,
        )


def make_context(
    server: FakeServer,
    *,
    elapsed: float,
    healths: dict[str, float] | None = None,
) -> EvaluatorContext:
    actual = (
        healths
        if healths is not None
        else {("Alice", "Bob", "Carol", "Dave", "Eve")[index]: 20 for index in range(4)}
    )

    def perception(name: str):
        if name not in actual:
            return None
        return {"self": {"health": actual[name], "inventory": {}}}

    return EvaluatorContext(
        server=server,
        perception=perception,
        elapsed_seconds=lambda: elapsed,
    )


def spec_with_params(base, **params) -> TaskSpec:
    _ = base
    spec = load_spec(SPEC_PATH)
    spec.params.update(params)
    if "generation_seed" in params:
        spec.metadata["generation_seed"] = params["generation_seed"]
    return spec


def item_summon_positions(commands: list[str]) -> list[tuple[float, float, float]]:
    positions: list[tuple[float, float, float]] = []
    for command in commands:
        match = re.match(
            r"summon minecraft:item\s+([-+]?\d+(?:\.\d+)?)\s+"
            r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s",
            command,
        )
        if match:
            positions.append(tuple(float(part) for part in match.groups()))
    return positions


class MonsterClearanceGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        template = load_generation_template(TEMPLATE_PATH)
        self.template = template
        self.generation_spec = generation_spec_from_template(template)

    def test_seed_is_reproducible_and_different_seed_changes_schedule(self) -> None:
        first = generate_spawn_schedule(self.generation_spec, 20260809)
        repeated = generate_spawn_schedule(self.generation_spec, 20260809)
        different = generate_spawn_schedule(self.generation_spec, 20260810)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)

    def test_generated_schedule_obeys_counts_times_bounds_and_distances(self) -> None:
        records = generate_spawn_schedule(self.generation_spec, 20260809)
        self.assertEqual({record.y for record in records}, {0})
        validate_spawn_schedule(
            records,
            bounds=self.generation_spec.bounds,
            timeout_seconds=180,
            expected_monster_count=16,
            initial_monster_count=4,
            last_spawn_deadline_seconds=90,
            monster_min_distance=8,
            agent_positions=self.generation_spec.agent_positions,
            initial_agent_min_distance=30,
            batch_interval_seconds=15,
        )
        monsters = [record for record in records if record.kind == "monster"]
        supplies = [record for record in records if record.kind == "item"]
        self.assertEqual(len(monsters), 16)
        self.assertEqual(sum(record.spawn_time_s == 0 for record in monsters), 4)
        self.assertEqual(
            {
                time: sum(record.spawn_time_s == time for record in monsters)
                for time in sorted({record.spawn_time_s for record in monsters})
            },
            {0.0: 4, 15.0: 2, 30.0: 2, 45.0: 2, 60.0: 2, 75.0: 2, 90.0: 2},
        )
        self.assertLessEqual(max(record.spawn_time_s for record in monsters), 90)
        self.assertEqual(
            sum(record.resource == "minecraft:zombie" for record in monsters),
            13,
        )
        self.assertEqual(
            sum(record.resource == "minecraft:skeleton" for record in monsters),
            3,
        )
        self.assertEqual(supplies, [])
        self.assertEqual(len({record.position for record in records}), len(records))
        initial_monsters = [
            record for record in monsters if record.spawn_time_s == 0
        ]
        for index, left in enumerate(initial_monsters):
            for right in initial_monsters[index + 1 :]:
                self.assertGreaterEqual(
                    math.hypot(left.x - right.x, left.z - right.z),
                    8,
                )

    def test_quick_weights_profiles_and_supply_budget(self) -> None:
        self.assertEqual(
            dict(self.generation_spec.monster_weights),
            {"minecraft:zombie": 80.0, "minecraft:skeleton": 20.0},
        )
        self.assertEqual(
            dict(self.generation_spec.zombie_profile_weights),
            {"normal": 80.0, "elite_iron": 20.0},
        )
        records = generate_spawn_schedule(self.generation_spec, 20260809)
        elite_count = sum(record.profile == "elite_iron" for record in records)
        self.assertGreaterEqual(elite_count, 1)
        self.assertLessEqual(elite_count, 2)
        supplies = [record for record in records if record.kind == "item"]
        allowed = {
            "minecraft:iron_sword",
            "minecraft:iron_helmet",
            "minecraft:iron_chestplate",
            "minecraft:iron_leggings",
            "minecraft:iron_boots",
            "minecraft:arrow",
            "minecraft:potion",
            "minecraft:bread",
        }
        self.assertEqual(supplies, [])
        self.assertEqual(self.generation_spec.supply_drop_min_distance, 10)
        self.assertEqual(self.generation_spec.supply_drop_max_distance, 30)
        self.assertEqual(self.generation_spec.supply_interval_seconds, 5)
        self.assertEqual(set(dict(self.generation_spec.supply_weights)), allowed)

    def test_batch_generation_writes_loadable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundles = generate_task_batch(
                TEMPLATE_PATH,
                count=2,
                base_seed=0,
                output_dir=directory,
            )
            self.assertEqual(len(bundles), 2)
            self.assertEqual(
                {bundle.task.parent.name for bundle in bundles},
                {"config"},
            )
            self.assertEqual(
                {bundle.task.name for bundle in bundles},
                {"0.yaml", "1.yaml"},
            )
            for bundle in bundles:
                manager = MonsterClearanceManager()
                manager.configure(load_spec(bundle.task))
                self.assertTrue(bundle.schedule.is_file())
                self.assertTrue(bundle.commands.is_file())
                self.assertNotIn(" item ", bundle.schedule.read_text(encoding="utf-8"))
                self.assertNotIn(
                    "summon minecraft:item",
                    bundle.commands.read_text(encoding="utf-8"),
                )
                self.assertIn("supply_weights", load_spec(bundle.task).params)
                self.assertEqual(bundle.task.stem, "0" if bundle is bundles[0] else "1")
                self.assertEqual(load_spec(bundle.task).task_id, "monster_clearance")

    def test_runtime_default_spec_configures_without_generated_files(self) -> None:
        spec = expand_task_args(["monster_clearance"])[0]
        self.assertEqual(spec.params["generation_seed"], 0)
        self.assertNotIn("schedule_file", spec.params)
        self.assertNotIn("command_file", spec.params)
        manager = MonsterClearanceManager(
            scheduler_factory=ImmediateScheduler,
            monotonic=lambda: 0.0,
            supply_thread_enabled=False,
        )
        manager.configure(spec)
        self.assertEqual(len(manager._records), 16)
        self.assertEqual(len(manager._command_plan.task), 16)
        self.assertEqual(manager._target_monster_count, 10)

    def test_single_monster_single_agent_bundle_configure(self) -> None:
        import yaml

        template = {
            "task_id_prefix": "monster_clearance_debug",
            "name_prefix": "Debug Monster Success",
            "task": {
                "category": "monster_clearance",
                "agents": {"count": 1, "names": ["Alice"]},
                "environment": {
                    "world": "base_env",
                    "observation_range": 20,
                    "gamemode": "creative",
                    "time": "midnight",
                    "weather": "clear",
                    "difficulty": "normal",
                    "world_spawn": {"x": 0, "y": 0, "z": 0},
                    "gamerules": {
                        "doDaylightCycle": False,
                        "doWeatherCycle": False,
                        "doMobSpawning": False,
                        "mobGriefing": False,
                        "doMobLoot": True,
                        "keepInventory": True,
                    },
                    "init_commands": ["clear @a"],
                },
                "initial_state": {
                    "per_agent": {
                        "inventory": {
                            "minecraft:stone_sword": 1,
                            "minecraft:bow": 1,
                            "minecraft:arrow": 16,
                            "minecraft:iron_boots": 1,
                            "minecraft:iron_chestplate": 1,
                        }
                    },
                    "by_agent": [
                        {"name": "Alice", "position": {"x": 0, "y": 0, "z": 0}}
                    ],
                },
                "completion": {
                    "timeout_seconds": 120,
                    "success": {"type": "all_task_monsters_eliminated"},
                    "failure": {"type": "all_agents_dead"},
                },
                "trials": {"count": 1},
                "output": {"dir": "data", "save_memory": True},
                "params": {"scheduler_poll_interval": 0.1},
                "metadata": {"preset": "debug_success"},
            },
            "generation": {
                "bounds": {
                    "min_x": -30,
                    "max_x": 30,
                    "min_y": 0,
                    "max_y": 0,
                    "min_z": -30,
                    "max_z": 30,
                },
                "initial_spawn_bounds": {
                    "min_x": -10,
                    "max_x": 10,
                    "min_y": 0,
                    "max_y": 0,
                    "min_z": -10,
                    "max_z": 10,
                },
                "monster_count": 1,
                "initial_monster_count": 1,
                "batch_size": 1,
                "batch_interval_seconds": 60,
                "last_spawn_deadline_seconds": 60,
                "monster_min_distance": 5,
                "initial_agent_min_distance": 5,
                "supply_interval_seconds": 120,
                "supply_drop_min_distance": 10,
                "supply_drop_max_distance": 30,
                "monsters": [
                    {"resource": "minecraft:zombie", "weight": 100},
                    {"resource": "minecraft:skeleton", "weight": 0},
                ],
                "zombie_profiles": [
                    {"profile": "normal", "weight": 100},
                    {"profile": "elite_iron", "weight": 0},
                ],
                "elite_zombies": {"min": 0, "max": 0},
                "supply_weights": [
                    {"resource": "minecraft:bread", "amount": 2, "weight": 100}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            template_path = Path(directory) / "template.yaml"
            template_path.write_text(
                yaml.safe_dump(template, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            bundles = generate_task_batch(
                template_path,
                count=1,
                base_seed=7,
                output_dir=directory,
            )
            spec = load_spec(bundles[0].task)
            self.assertEqual(spec.agents["count"], 1)
            self.assertEqual(spec.environment["gamemode"], "creative")
            manager = MonsterClearanceManager()
            manager.configure(spec)
            monsters = [
                record
                for record in manager._records
                if record.kind == "monster"
            ]
            self.assertEqual(len(monsters), 1)
            self.assertEqual(monsters[0].resource, "minecraft:zombie")

    def test_duplicate_coordinate_is_rejected(self) -> None:
        records = generate_spawn_schedule(self.generation_spec, 20260809)
        bad = list(records)
        bad[1] = type(bad[1])(
            **{
                **bad[1].__dict__,
                "x": bad[0].x,
                "y": bad[0].y,
                "z": bad[0].z,
            }
        )
        with self.assertRaises(MonsterClearanceScheduleError):
            validate_spawn_schedule(bad)


class MonsterClearanceConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        template = load_generation_template(TEMPLATE_PATH)
        self.records = generate_spawn_schedule(
            generation_spec_from_template(template),
            20260809,
        )

    def test_commands_are_persistent_spawn_only_and_track_success(self) -> None:
        text = command_file_text(self.records)
        self.assertIn("execute store success score #m0001 acTaskSpawn", text)
        self.assertIn("PersistenceRequired:1b", text)
        self.assertNotIn("summon minecraft:item", text)
        self.assertNotIn("Age:-32768s", text)
        self.assertNotIn('id:"minecraft:potion"', text)
        self.assertNotIn('id:"minecraft:splash_potion"', text)
        self.assertNotIn("setblock", text)
        self.assertNotIn(" kill ", text)

    def test_elite_zombie_uses_one_to_three_random_iron_items(self) -> None:
        text = command_file_text(self.records)
        elite_records = [
            record
            for record in self.records
            if record.resource == "minecraft:zombie"
            and record.profile == "elite_iron"
        ]
        self.assertGreaterEqual(len(elite_records), 1)
        self.assertLessEqual(len(elite_records), 2)
        equipment = (
            "minecraft:iron_sword",
            "minecraft:iron_helmet",
            "minecraft:iron_chestplate",
            "minecraft:iron_leggings",
            "minecraft:iron_boots",
        )
        for record in elite_records:
            line = next(
                line for line in text.splitlines() if f"#{record.spawn_id} " in line
            )
            item_count = sum(f'id:"{item}"' in line for item in equipment)
            self.assertGreaterEqual(item_count, 1)
            self.assertLessEqual(item_count, 3)
            self.assertIn("HandDropChances:", line)
            self.assertIn("ArmorDropChances:", line)

    def test_builtin_command_file_matches_schedule(self) -> None:
        manager = MonsterClearanceManager()
        manager.configure(load_spec(SPEC_PATH))
        self.assertEqual(len(manager._records), 16)
        self.assertEqual(len(manager._monster_records), 16)
        self.assertEqual({record.y for record in manager._records}, {0})
        self.assertEqual(len(manager._command_plan.task), 16)
        self.assertTrue(
            all("summon minecraft:item" not in entry.command for entry in manager._command_plan.task)
        )


class MonsterClearanceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        ImmediateScheduler.next_errors = []
        ImmediateScheduler.sent_count = None
        self.clock = FakeClock()
        registry = create_default_registry()
        self.spec = registry.get("monster_clearance")
        self.manager = MonsterClearanceManager(
            scheduler_factory=ImmediateScheduler,
            monotonic=self.clock.monotonic,
            supply_thread_enabled=False,
        )
        self.manager.configure(self.spec)
        self.server = FakeServer()
        self.agents = [
            FakeAgent(
                ("Alice", "Bob", "Carol", "Dave", "Eve")[index],
                position={
                    "x": (-2.0, 2.0, -2.0, 2.0)[index],
                    "y": 0.0,
                    "z": (-2.0, -2.0, 2.0, 2.0)[index],
                },
            )
            for index in range(4)
        ]

    def tearDown(self) -> None:
        self.manager.teardown()

    def _prepare_and_start(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        self.manager.start()
        if self.manager._scheduler_thread is not None:
            self.manager._scheduler_thread.join(timeout=1)

    def test_prepare_disables_natural_spawns_and_places_loadout_in_backpack(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        commands = self.server.commands
        self.assertIn("setworldspawn 0 0 0", commands)
        self.assertIn("fill -50 0 -50 50 2 50 air replace", commands)
        self.assertIn("tp Alice -2 0 -2", commands)
        self.assertIn("gamerule doMobSpawning false", commands)
        self.assertIn("team add ac_monster_clearance", commands)
        self.assertIn("team modify ac_monster_clearance friendlyFire false", commands)
        for name in ("Alice", "Bob", "Carol", "Dave"):
            self.assertIn(f"team join ac_monster_clearance {name}", commands)
        clear_field = (
            "kill @e[type=!player,x=-50,y=0,z=-50,dx=100,dy=2,dz=100]"
        )
        self.assertIn("forceload add -4 -4 3 3", commands)
        self.assertIn("forceload remove all", commands)
        self.assertIn(clear_field, commands)
        self.assertLess(
            commands.index("forceload add -4 -4 3 3"),
            commands.index(clear_field),
        )
        self.assertLess(
            commands.index(clear_field),
            commands.index("forceload remove all"),
        )
        self.assertLess(
            commands.index(clear_field),
            commands.index("tp Alice -2 0 -2"),
        )
        self.assertIn(
            "item replace entity Alice inventory.0 with minecraft:stone_sword 1",
            commands,
        )
        self.assertIn(
            "item replace entity Alice inventory.1 with minecraft:bow 1",
            commands,
        )
        self.assertIn(
            "item replace entity Alice inventory.2 with minecraft:arrow 16",
            commands,
        )
        self.assertIn(
            "item replace entity Alice inventory.3 with minecraft:iron_boots 1",
            commands,
        )
        self.assertIn(
            "item replace entity Alice inventory.4 with minecraft:iron_chestplate 1",
            commands,
        )
        self.assertFalse(
            any(command == "give Alice minecraft:stone_sword 1" for command in commands)
        )
        self.assertFalse(
            any(command == "give Alice minecraft:bow 1" for command in commands)
        )
        self.assertNotIn(
            "item replace entity Alice inventory.3 with minecraft:iron_leggings 1",
            commands,
        )
        self.assertNotIn(
            "item replace entity Alice inventory.3 with minecraft:iron_helmet 1",
            commands,
        )
        self.assertIn("scoreboard objectives add acTaskDeath deathCount", commands)
        self.assertIn("scoreboard objectives add acAlive dummy", commands)

    def test_future_spawns_prevent_early_success(self) -> None:
        ImmediateScheduler.sent_count = 3
        self.manager.configure(self.spec)
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=1))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertFalse(outcome.detail["schedule_complete"])

    def test_all_monsters_eliminated_with_alive_agent_succeeds_and_latches(self) -> None:
        self.server.monster_health = {
            f"m{index:04d}": 20 for index in range(11, 17)
        }
        self._prepare_and_start()
        context = make_context(self.server, elapsed=130)
        outcomes = []
        for _ in range(8):
            outcomes.append(self.manager.completion_check(context))
            self.clock.advance(15)
        outcome = outcomes[-1]
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.detail["eliminated_monsters"], 10)
        self.assertEqual(outcome.detail["planned_monsters"], 16)
        self.assertEqual(outcome.detail["target_monsters"], 10)
        self.assertEqual(outcome.detail["alive_monsters"], 6)
        cleanup_command = "kill @e[tag=ac_mon_0]"
        self.assertEqual(self.server.priority_commands.count(cleanup_command), 1)
        self.server.monster_health = {"m0001": 20}
        self.assertIs(self.manager.completion_check(context), outcome)
        self.assertEqual(self.server.priority_commands.count(cleanup_command), 1)

    def test_spawn_score_zero_causes_failure(self) -> None:
        self.server.spawn_scores["m0001"] = 0
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=1))
        self.assertEqual(outcome.status, TaskStatus.FAILURE)
        self.assertEqual(outcome.reason, "spawn_failed")

    def test_scheduler_error_causes_failure(self) -> None:
        ImmediateScheduler.next_errors = [
            (CommandEntry(0, "s", "summon zombie 0 1 0", 1), "RCON failed")
        ]
        self.manager.configure(self.spec)
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=1))
        self.assertEqual(outcome.status, TaskStatus.FAILURE)
        self.assertEqual(outcome.reason, "schedule_failed")

    def test_alive_query_error_does_not_report_success(self) -> None:
        self.server.alive_query_errors.add("m0001")
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=130))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertIn("query_note", outcome.detail)

    def test_killed_monsters_eliminated_before_schedule_completes(self) -> None:
        ImmediateScheduler.sent_count = 1
        self.manager.configure(self.spec)
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=130))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertFalse(outcome.detail["schedule_complete"])
        self.assertEqual(outcome.detail["eliminated_monsters"], 1)
        self.assertEqual(outcome.detail["alive_monsters"], 0)

    def test_unsent_monster_is_not_eliminated_or_failed(self) -> None:
        ImmediateScheduler.sent_count = 1
        self.manager.configure(self.spec)
        self.server.spawn_scores["m0002"] = 0
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=130))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertEqual(outcome.detail["eliminated_monsters"], 1)

    def test_empty_field_waits_for_batch_interval(self) -> None:
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=0))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertEqual(self.manager._next_batch_index, 1)

        self.clock.advance(15)
        outcome = self.manager.completion_check(make_context(self.server, elapsed=15))
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertEqual(self.manager._next_batch_index, 2)
        self.assertEqual(
            [
                re.search(r"#(m\d{4}) ", entry.command).group(1)
                for entry in self.manager._scheduler.plan.task
            ],
            ["m0005", "m0006"],
        )

    def test_live_field_waits_for_batch_interval(self) -> None:
        self.server.monster_health = {
            f"m{index:04d}": 20 for index in range(1, 5)
        }
        self._prepare_and_start()

        first = self.manager.completion_check(make_context(self.server, elapsed=0))
        self.assertEqual(first.status, TaskStatus.RUNNING)
        self.assertEqual(self.manager._next_batch_index, 1)

        self.clock.advance(14.9)
        before_interval = self.manager.completion_check(
            make_context(self.server, elapsed=14.9)
        )
        self.assertEqual(before_interval.status, TaskStatus.RUNNING)
        self.assertEqual(self.manager._next_batch_index, 1)

        self.clock.advance(0.1)
        at_interval = self.manager.completion_check(
            make_context(self.server, elapsed=15)
        )
        self.assertEqual(at_interval.status, TaskStatus.RUNNING)
        self.assertEqual(self.manager._next_batch_index, 2)

    def test_missing_agent_perception_does_not_report_success(self) -> None:
        self._prepare_and_start()
        outcome = self.manager.completion_check(
            make_context(self.server, elapsed=130, healths={})
        )
        self.assertEqual(outcome.status, TaskStatus.RUNNING)

    def test_first_death_eliminates_agent_and_all_deaths_fail(self) -> None:
        self.server.monster_health = {
            record.spawn_id: 20 for record in self.manager._monster_records
        }
        self._prepare_and_start()
        self.server.death_scores["Alice"] = 1
        running = self.manager.completion_check(make_context(self.server, elapsed=50))
        self.assertEqual(running.status, TaskStatus.RUNNING)
        self.assertIn("Alice", running.detail["eliminated_agents"])
        self.assertIn(
            "gamemode spectator @a[name=Alice,scores={acTaskDeath=1..}]",
            self.server.commands,
        )

        self.server.death_scores.update(
            {("Alice", "Bob", "Carol", "Dave", "Eve")[index]: 1 for index in range(1, 4)}
        )
        failed = self.manager.completion_check(make_context(self.server, elapsed=60))
        self.assertEqual(failed.status, TaskStatus.FAILURE)
        self.assertEqual(failed.reason, "all_agents_dead")

    def test_health_fallback_eliminates_agent_when_death_score_query_fails(self) -> None:
        self._prepare_and_start()
        self.server.fail_on = "scoreboard players get Alice acTaskDeath"

        running = self.manager.completion_check(
            make_context(self.server, elapsed=50, healths={"Alice": 0})
        )

        self.assertEqual(running.status, TaskStatus.RUNNING)
        self.assertNotIn("Alice", self.manager._eliminated_agents)
        self.assertNotIn(
            "gamemode spectator @a[name=Alice,scores={acTaskDeath=1..}]",
            self.server.commands,
        )

    def test_supply_drop_seed_is_reproducible(self) -> None:
        first = supply_drop_signature(self.spec, 42)
        repeated = supply_drop_signature(self.spec, 42)
        different = supply_drop_signature(self.spec, 43)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)

    def test_generated_agent_spawns_are_inside_center_radius(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundles = generate_task_batch(
                TEMPLATE_PATH,
                count=2,
                base_seed=0,
                output_dir=directory,
            )
            generated = [load_spec(bundle.task) for bundle in bundles]
            for spec in generated:
                center = spec.environment["world_spawn"]
                positions = spec.initial_state["by_agent"]
                self.assertEqual(len(positions), 4)
                for block in positions:
                    position = block["position"]
                    distance = math.hypot(
                        position["x"] - center["x"],
                        position["z"] - center["z"],
                    )
                    self.assertLessEqual(distance, 10.0)
            self.assertNotEqual(
                generated[0].initial_state["by_agent"],
                generated[1].initial_state["by_agent"],
            )

    def test_runtime_supply_drops_near_alive_agent(self) -> None:
        self.agents[0].health = 0.0
        self.agents[1].position = {"x": 10.0, "y": 0.0, "z": 10.0}
        self.agents[2].game_mode = "spectator"
        self.agents[3].position = {"x": -10.0, "y": 0.0, "z": -10.0}
        self._prepare_and_start()

        self.manager._run_supply_step(self.clock.monotonic())

        metrics = self.manager.metrics(make_context(self.server, elapsed=0))
        self.assertEqual(metrics["supply_attempts"], 1)
        self.assertEqual(metrics["spawned_supplies"], 1)
        self.assertIn(metrics["last_supply_drop"]["agent"], {"Bob", "Dave"})
        position = item_summon_positions(self.server.commands)[0]
        origin = next(
            agent.position
            for agent in self.agents
            if agent.username == metrics["last_supply_drop"]["agent"]
        )
        distance = math.hypot(position[0] - origin["x"], position[2] - origin["z"])
        self.assertGreaterEqual(distance, 10.0)
        self.assertLessEqual(distance, 30.0)
        self.assertIn('Tags:["ac_sup_0","ac_supply_0001"]', self.server.commands[-1])

    def test_runtime_supply_uses_interval(self) -> None:
        self._prepare_and_start()
        self.manager._run_supply_step(self.clock.monotonic())
        self.clock.advance(4.9)
        self.manager._run_supply_step(self.clock.monotonic())
        self.assertEqual(
            self.manager.metrics(make_context(self.server, elapsed=4.9))["spawned_supplies"],
            1,
        )

        self.clock.advance(0.1)
        self.manager._run_supply_step(self.clock.monotonic())

        metrics = self.manager.metrics(make_context(self.server, elapsed=5.0))
        self.assertEqual(metrics["supply_attempts"], 2)
        self.assertEqual(metrics["spawned_supplies"], 2)

    def test_runtime_supply_skips_when_no_alive_agent(self) -> None:
        for agent in self.agents:
            agent.health = 0.0
        self._prepare_and_start()

        self.manager._run_supply_step(self.clock.monotonic())

        metrics = self.manager.metrics(make_context(self.server, elapsed=0))
        self.assertEqual(metrics["spawned_supplies"], 0)
        self.assertEqual(metrics["supply_skipped_no_alive_agent"], 1)
        self.assertFalse(item_summon_positions(self.server.commands))

    def test_supply_rcon_failure_causes_task_failure(self) -> None:
        manager = MonsterClearanceManager(
            scheduler_factory=ImmediateScheduler,
            monotonic=self.clock.monotonic,
            supply_thread_enabled=False,
        )
        manager.configure(self.spec)
        server = FakeServer(fail_on="summon minecraft:item")
        manager.prepare(server, self.agents, memory=None)
        manager.start()
        if manager._scheduler_thread is not None:
            manager._scheduler_thread.join(timeout=1)

        manager._run_supply_step(self.clock.monotonic())
        outcome = manager.completion_check(make_context(server, elapsed=1))

        self.assertEqual(outcome.status, TaskStatus.FAILURE)
        self.assertEqual(outcome.reason, "schedule_failed")
        self.assertIn("supply_error", outcome.detail)
        manager.teardown()

    def test_timeout_when_monsters_remain(self) -> None:
        self.server.monster_health = {
            record.spawn_id: 20 for record in self.manager._monster_records
        }
        self._prepare_and_start()
        outcome = self.manager.completion_check(make_context(self.server, elapsed=180))
        self.assertEqual(outcome.status, TaskStatus.TIMEOUT)
        self.assertEqual(outcome.detail["alive_monsters"], 4)

    def test_teardown_cancels_scheduler_and_cleans_task_entities(self) -> None:
        self._prepare_and_start()
        scheduler = self.manager._scheduler
        self.manager.teardown()
        self.assertTrue(scheduler.cancelled)
        self.assertIn("kill @e[tag=ac_mon_0]", self.server.commands)
        self.assertIn("kill @e[tag=ac_sup_0]", self.server.commands)
        self.assertIn("scoreboard objectives remove acTaskDeath", self.server.commands)
        self.assertIn("scoreboard objectives remove acAlive", self.server.commands)


def supply_drop_signature(base_spec, seed: int) -> tuple:
    clock = FakeClock()
    spec = spec_with_params(base_spec, generation_seed=seed)
    manager = MonsterClearanceManager(
        scheduler_factory=ImmediateScheduler,
        monotonic=clock.monotonic,
        supply_thread_enabled=False,
    )
    server = FakeServer()
    agents = [
        FakeAgent("Alice", position={"x": 0.0, "y": 0.0, "z": 0.0}),
        FakeAgent("Bob", position={"x": 5.0, "y": 0.0, "z": 5.0}),
        FakeAgent("Carol", position={"x": -5.0, "y": 0.0, "z": 5.0}),
        FakeAgent("Dave", position={"x": 5.0, "y": 0.0, "z": -5.0}),
    ]
    manager.configure(spec)
    manager.prepare(server, agents, memory=None)
    manager.start()
    if manager._scheduler_thread is not None:
        manager._scheduler_thread.join(timeout=1)
    manager._run_supply_step(clock.monotonic())
    signature = (
        tuple(item_summon_positions(server.commands)),
        manager.metrics(make_context(server, elapsed=0))["last_supply_drop"],
    )
    manager.teardown()
    return signature


if __name__ == "__main__":
    unittest.main()
