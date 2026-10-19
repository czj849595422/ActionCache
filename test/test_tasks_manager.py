#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/tasks 任务管理器：环境装配与真实数据完成判定测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    BaseTaskManager,
    BossCombatManager,
    EvaluatorContext,
    TaskManager,
    TaskSpec,
    TaskSpecError,
    TaskStatus,
    create_default_registry,
)
from src.tasks.boss_combat.arena import BOSS_ARENA_WALL_COMMANDS  # noqa: E402

SPEC_PATH = ROOT / "src" / "tasks" / "specs" / "boss_combat.yaml"
# Minecraft UUID int 数组 [I; a,b,c,d] 映射为两段 long: (a<<32|b, c<<32|d)
BOSS_UUID = "00000001-0000-0002-0000-000300000004"


class FakeServer:
    def __init__(
        self,
        responses: dict[str, str] | None = None,
        raise_commands: set[str] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.responses = dict(responses or {})
        self.raise_commands = set(raise_commands or set())

    def send_command(self, command: str, response_timeout: float = 5.0) -> str:
        self.commands.append(command)
        if command in self.raise_commands:
            raise RuntimeError("RCON failed")
        if command in self.responses:
            return self.responses[command]
        if command.startswith("gamemode spectator "):
            name = command.rsplit(" ", 1)[-1]
            return f"Set {name}'s game mode to Spectator Mode"
        return ""


class FakeAgent:
    def __init__(self, username: str) -> None:
        self.username = username


def make_perception(
    healths: dict[str, float],
    inventories: dict[str, dict[str, int]] | None = None,
    game_modes: dict[str, str] | None = None,
):
    snapshots: dict[str, dict[str, object]] = {}
    agents = set(healths) | set((inventories or {}).keys()) | set(
        (game_modes or {}).keys()
    )
    for agent in agents:
        self_state: dict[str, object] = {
            "health": healths.get(agent, 20.0),
            "inventory": (inventories or {}).get(agent, {}),
            "gameMode": (game_modes or {}).get(agent, "survival"),
        }
        if game_modes and agent in game_modes:
            self_state["gameMode"] = game_modes[agent]
        snapshots[agent] = {
            "self": self_state,
        }
    return lambda agent: snapshots.get(agent)


def make_context(
    server: FakeServer,
    *,
    elapsed: float = 0.0,
    healths: dict[str, float] | None = None,
    inventories: dict[str, dict[str, int]] | None = None,
    game_modes: dict[str, str] | None = None,
) -> EvaluatorContext:
    state = {"elapsed": elapsed}
    return EvaluatorContext(
        server=server,
        perception=make_perception(healths or {}, inventories, game_modes),
        elapsed_seconds=lambda: state["elapsed"],
    )


class BossCombatPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = create_default_registry()
        self.spec = registry.get("boss_combat")
        self.manager = BossCombatManager()
        self.manager.configure(self.spec)

    def _boss_server(self) -> FakeServer:
        return FakeServer(
            responses={
                "data get entity @e[type=wither,limit=1] UUID": (
                    "Steve has the following entity data: [I; 1, 2, 3, 4]"
                ),
                f"data get entity {BOSS_UUID} Health": (
                    "Steve has the following entity data: 300.0f"
                ),
                f"attribute {BOSS_UUID} minecraft:generic.max_health get": (
                    "Value of attribute Max Health for entity Wither is 300.0"
                ),
            }
        )

    def test_prepare_generates_expected_command_sequence(self) -> None:
        server = self._boss_server()
        agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave")[index]) for index in range(4)]
        self.manager.prepare(server, agents, memory=None)

        commands = server.commands
        self.assertEqual(
            commands[:3],
            ["time set midnight", "weather clear", "difficulty easy"],
        )
        self.assertIn("gamemode survival Alice", commands)
        self.assertNotIn("gamemode survival Eve", commands)
        self.assertIn("gamerule doMobSpawning false", commands)
        self.assertIn("gamerule keepInventory true", commands)
        self.assertIn("gamerule mobGriefing false", commands)
        self.assertIn("team add ac_boss_combat", commands)
        self.assertIn("team modify ac_boss_combat friendlyFire false", commands)
        for command in BOSS_ARENA_WALL_COMMANDS:
            self.assertIn(command, commands)
        for name in ("Alice", "Bob", "Carol", "Dave"):
            self.assertIn(f"team join ac_boss_combat {name}", commands)
        self.assertIn("setworldspawn 5 0 5", commands)
        self.assertNotIn("fill 0 -1 0 10 -1 10 minecraft:stone", commands)
        self.assertIn(
            'give Alice diamond_sword{Enchantments:[{id:"minecraft:smite",lvl:3s}]} 1',
            commands,
        )
        expected_equipment = {
            'bow{Enchantments:[{id:"minecraft:power",lvl:3s}]}': "weapon.mainhand",
            'diamond_helmet{Enchantments:[{id:"minecraft:protection",lvl:3s}]}': "armor.head",
            'diamond_chestplate{Enchantments:[{id:"minecraft:protection",lvl:3s}]}': "armor.chest",
            'diamond_leggings{Enchantments:[{id:"minecraft:protection",lvl:3s}]}': "armor.legs",
            'diamond_boots{Enchantments:[{id:"minecraft:protection",lvl:3s},{id:"minecraft:feather_falling",lvl:3s}]}': "armor.feet",
        }
        for item, slot in expected_equipment.items():
            command = f"item replace entity Alice {slot} with {item} 1"
            self.assertIn(command, commands)
            self.assertNotIn(f"give Alice {item} 1", commands)
        for name in ("Alice", "Bob", "Carol", "Dave"):
            for item, slot in expected_equipment.items():
                self.assertIn(
                    f"item replace entity {name} {slot} with {item} 1",
                    commands,
                )
        self.assertNotIn("give Alice bow 1", commands)
        self.assertIn("give Alice arrow 200", commands)
        self.assertIn("give Alice golden_apple 5", commands)
        self.assertIn(
            'give Alice minecraft:potion{Potion:"minecraft:healing"} 10',
            commands,
        )
        self.assertIn("tp Dave 0 0 0", commands)
        # 初始实体不在 prepare 阶段召唤，由 spawn_initial_entities 统一处理。
        self.assertNotIn(
            "summon wither 18.784 1.0 -26.751 "
            "{ActiveEffects:[{Id:2b,Amplifier:200b,Duration:1000000,Ambient:1b}]}",
            commands,
        )
        self.assertNotIn("data get entity @e[type=wither,limit=1] UUID", commands)

        self.manager.spawn_initial_entities(server)
        commands = server.commands
        self.assertIn("kill @e[type=minecraft:wither]", commands)
        self.assertIn("kill @e[type=minecraft:zombie]", commands)
        self.assertIn(
            "summon wither 18.784 1.0 -26.751 "
            "{ActiveEffects:[{Id:2b,Amplifier:200b,Duration:1000000,Ambient:1b}]}",
            commands,
        )
        self.assertIn("data get entity @e[type=wither,limit=1] UUID", commands)
        self.assertIn(f"data get entity {BOSS_UUID} Health", commands)
        self.assertIn(
            f"attribute {BOSS_UUID} minecraft:generic.max_health get",
            commands,
        )
        self.assertEqual(self.manager.boss_uuids, [BOSS_UUID])
        self.assertEqual(self.manager.boss_max_health, 300.0)

    def test_prepare_ignores_memory_chat(self) -> None:
        server_with = self._boss_server()
        server_without = self._boss_server()
        agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave")[index]) for index in range(4)]
        memory_with_chat = type(
            "FakeMemory",
            (),
            {"chat": [{"sender": "SystemInfo", "message": "Boss Health: 300"}]},
        )()
        self.manager.prepare(server_with, agents, memory=memory_with_chat)
        self.manager.prepare(server_without, agents, memory=None)
        self.assertEqual(server_with.commands, server_without.commands)

    def test_supply_drop_uses_alive_agent_position(self) -> None:
        data = self.spec.to_dict()
        data["params"]["supply_drop_items"] = [
            {"item": "minecraft:arrow", "amount": 32, "weight": 1.0}
        ]
        data["params"]["supply_drop_min_distance"] = 10
        data["params"]["supply_drop_max_distance"] = 10
        spec = TaskSpec.from_dict(data)
        manager = BossCombatManager()
        manager.configure(spec)
        server = FakeServer(
            responses={
                "data get entity Alice Health": (
                    "Alice has the following entity data: 20.0f"
                ),
                "data get entity Alice Pos": (
                    "Alice has the following entity data: [0.0d, 0.0d, 0.0d]"
                ),
                "data get entity Bob Health": (
                    "Bob has the following entity data: 0.0f"
                ),
                "data get entity Carol Health": (
                    "Carol has the following entity data: 0.0f"
                ),
                "data get entity Dave Health": (
                    "Dave has the following entity data: 0.0f"
                ),
            }
        )
        agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave")[index]) for index in range(4)]
        manager.prepare(server, agents, memory=None)

        self.assertTrue(manager._drop_supply_once())

        summon = [
            command
            for command in server.commands
            if command.startswith("summon minecraft:item")
        ][0]
        self.assertIn('Tags:["ac_boss_supply"]', summon)
        self.assertIn('Item:{id:"minecraft:arrow",Count:32b}', summon)
        self.assertIn("data get entity Alice Health", server.commands)
        self.assertIn("data get entity Alice Pos", server.commands)
        metrics = manager.metrics(make_context(server, healths={"Alice": 20.0}))
        self.assertEqual(metrics["supply_drops_sent"], 1)
        self.assertEqual(metrics["last_supply_drop"]["agent"], "Alice")


class BossCombatCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = create_default_registry()
        self.spec = registry.get("boss_combat")
        self.manager = BossCombatManager()
        self.manager.configure(self.spec)
        self.manager.boss_max_health = 300.0
        self.manager.boss_uuids = [BOSS_UUID]

    def _health_server(self, health_response: str) -> FakeServer:
        return FakeServer(
            responses={f"data get entity {BOSS_UUID} Health": health_response}
        )

    def _status_server(
        self,
        *,
        health_response: str = "Steve has the following entity data: 150.0f",
        position_response: str = (
            "Steve has the following entity data: [8.0d, 1.0d, -3.5d]"
        ),
    ) -> FakeServer:
        return FakeServer(
            responses={
                f"data get entity {BOSS_UUID} Health": health_response,
                f"data get entity {BOSS_UUID} Pos": position_response,
            }
        )

    def test_running_while_boss_alive(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        context = make_context(server, healths={"Alice": 20.0})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.RUNNING)

    def test_metrics_expose_boss_uuid_separately(self) -> None:
        server = self._status_server()
        context = EvaluatorContext(
            server=server,
            perception=lambda _name: {
                "entities": [
                    {
                        "name": "wither",
                        "id": 70,
                        "uuid": BOSS_UUID,
                    }
                ]
            },
            elapsed_seconds=lambda: 0.0,
        )
        metrics = self.manager.metrics(context)

        self.assertEqual(metrics["boss_uuid"], BOSS_UUID)
        self.assertEqual(metrics["boss_entity_id"], 70)

    def test_success_when_boss_health_below_ratio(self) -> None:
        server = self._health_server("Steve has the following entity data: 0.0f")
        context = make_context(server, healths={"Alice": 20.0})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.reason, "boss_health_below")
        self.assertEqual(outcome.detail["health"], 0.0)

    def test_success_when_boss_entity_gone(self) -> None:
        server = self._health_server("No entity was found")
        context = make_context(server, healths={"Alice": 20.0})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)

    def test_failure_when_all_agents_dead(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        context = make_context(
            server,
            healths={"Alice": 20.0, "Bob": 20.0, "Carol": 20.0, "Dave": 20.0},
            game_modes={
                "Alice": "spectator",
                "Bob": "spectator",
                "Carol": "spectator",
                "Dave": "spectator",
            },
        )
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.FAILURE)
        self.assertEqual(outcome.reason, "all_agents_dead")
        self.assertIn(
            "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]",
            server.commands,
        )

    def test_first_death_switches_to_spectator_and_drops_loot(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        snapshots = {
            "Alice": {
                "self": {
                    "health": 20.0,
                    "gameMode": "spectator",
                    "inventory": [
                        {"id": "minecraft:arrow", "count": 65, "slot": 0},
                        {"id": "minecraft:golden_apple", "count": 2, "slot": 1},
                    ],
                    "equipmentSlots": {
                        "mainHand": {"id": "minecraft:diamond_sword", "count": 1},
                        "chest": {"id": "minecraft:diamond_chestplate", "count": 1},
                    },
                    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                }
            },
            "Bob": {"self": {"health": 20.0}},
            "Carol": {"self": {"health": 20.0}},
            "Dave": {"self": {"health": 20.0}},
        }
        context = EvaluatorContext(
            server=server,
            perception=lambda name: snapshots.get(name),
            elapsed_seconds=lambda: 0.0,
        )

        outcome = self.manager.completion_check(context)

        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertIn(
            "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]",
            server.commands,
        )
        self.assertIn("clear Alice", server.commands)
        drops = [command for command in server.commands if command.startswith("summon minecraft:item")]
        self.assertTrue(any('Item:{id:"minecraft:diamond_sword",Count:1b}' in command for command in drops))
        self.assertTrue(any('Item:{id:"minecraft:diamond_chestplate",Count:1b}' in command for command in drops))
        self.assertTrue(any('Item:{id:"minecraft:arrow",Count:64b}' in command for command in drops))
        self.assertTrue(any('Item:{id:"minecraft:arrow",Count:1b}' in command for command in drops))
        self.assertEqual(
            server.commands.count(
                "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]"
            ),
            1,
        )
        metrics = self.manager.metrics(context)
        self.assertNotIn("Alice", metrics["alive_agents"])

    def test_death_scoreboard_triggers_elimination_after_respawn(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        server.responses[
            "scoreboard players get Alice ac_boss_death_count"
        ] = "Alice has the following score: 1"
        context = make_context(
            server,
            healths={"Alice": 20.0, "Bob": 20.0, "Carol": 20.0, "Dave": 20.0},
        )

        outcome = self.manager.completion_check(context)

        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertIn(
            "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]",
            server.commands,
        )
        self.assertIn("clear Alice", server.commands)
        metrics = self.manager.metrics(context)
        self.assertNotIn("Alice", metrics["alive_agents"])

    def test_custom_runtime_roster_is_used_for_all_death_checks(self) -> None:
        data = self.spec.to_dict()
        data["agents"] = {"count": 2, "names": ["Red", "Blue"]}
        manager = BossCombatManager()
        manager.configure(TaskSpec.from_dict(data))
        server = self._health_server("Steve has the following entity data: 300.0f")
        manager.prepare(server, [FakeAgent("Red"), FakeAgent("Blue")], memory=None)
        server.responses["scoreboard players get Red ac_boss_death_count"] = (
            "Red has the following score: 1"
        )
        server.responses["scoreboard players get Blue ac_boss_death_count"] = (
            "Blue has the following score: 1"
        )
        context = make_context(server, healths={"Red": 0.0, "Blue": 0.0})

        outcome = manager.completion_check(context)

        self.assertEqual(outcome.reason, "all_agents_dead")
        self.assertIn(
            "gamemode spectator @a[name=Red,scores={ac_boss_death_count=1..}]",
            server.commands,
        )
        self.assertIn(
            "gamemode spectator @a[name=Blue,scores={ac_boss_death_count=1..}]",
            server.commands,
        )
        self.assertNotIn(
            "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]",
            server.commands,
        )

    def test_failed_spectator_command_is_retried_and_reported(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        server.responses["scoreboard players get Alice ac_boss_death_count"] = (
            "Alice has the following score: 1"
        )
        server.raise_commands.add(
            "gamemode spectator @a[name=Alice,scores={ac_boss_death_count=1..}]"
        )
        context = make_context(server, healths={"Alice": 0.0})

        outcomes = [self.manager.completion_check(context) for _ in range(3)]

        self.assertEqual(outcomes[-1].status, TaskStatus.FAILURE)
        self.assertEqual(outcomes[-1].reason, "agent_elimination_failed")
        self.assertNotIn("Alice", self.manager._eliminated_agents)
        self.assertIn("Alice", self.manager._pending_eliminations)

    def test_timeout(self) -> None:
        server = self._health_server("Steve has the following entity data: 300.0f")
        context = make_context(
            server,
            elapsed=601.0,
            healths={"Alice": 20.0},
        )
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.TIMEOUT)

    def test_query_failure_returns_running_with_detail(self) -> None:
        server = self._health_server("")
        server.raise_commands.add(f"data get entity {BOSS_UUID} Health")
        context = make_context(server, healths={"Alice": 20.0})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.RUNNING)
        self.assertEqual(outcome.detail.get("note"), "BOSS 血量查询失败")

    def test_query_failure_still_times_out(self) -> None:
        server = self._health_server("")
        server.raise_commands.add(f"data get entity {BOSS_UUID} Health")
        context = make_context(server, elapsed=601.0, healths={"Alice": 20.0})
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.TIMEOUT)

    def test_metrics_report_boss_progress_and_surviving_agents(self) -> None:
        server = self._health_server("Steve has the following entity data: 150.0f")
        context = make_context(
            server,
            elapsed=25.0,
            healths={"Alice": 20.0, "Bob": 0.0},
            game_modes={"Bob": "spectator"},
        )
        metrics = self.manager.metrics(context)
        self.assertEqual(metrics["current_health"], 150.0)
        self.assertEqual(metrics["max_health"], 300.0)
        self.assertEqual(metrics["current_ratio"], 0.5)
        self.assertEqual(metrics["target_ratio"], 0.0)
        self.assertEqual(metrics["alive_agents"], ["Alice"])
        self.assertEqual(metrics["remaining_seconds"], 275.0)

    def test_system_message_reports_boss_position_and_health(self) -> None:
        now = {"value": 100.0}
        server = self._status_server()
        context = EvaluatorContext(
            server=server,
            perception=make_perception({"Alice": 20.0}),
            elapsed_seconds=lambda: 0.0,
            now=lambda: now["value"],
        )

        message = self.manager.system_message(context)

        self.assertEqual(
            message,
            "Boss wither：位于(8, 1, -3.5)； 血量150。",
        )
        self.assertIn(f"data get entity {BOSS_UUID} Health", server.commands)
        self.assertIn(f"data get entity {BOSS_UUID} Pos", server.commands)

    def test_system_message_is_throttled_to_twenty_seconds(self) -> None:
        now = {"value": 100.0}
        server = self._status_server()
        context = EvaluatorContext(
            server=server,
            perception=make_perception({"Alice": 20.0}),
            elapsed_seconds=lambda: 0.0,
            now=lambda: now["value"],
        )

        first = self.manager.system_message(context)
        second = self.manager.system_message(context)
        now["value"] = 119.9
        third = self.manager.system_message(context)
        now["value"] = 120.0
        fourth = self.manager.system_message(context)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        self.assertEqual(fourth, first)

    def test_system_message_returns_none_when_boss_status_unavailable(self) -> None:
        server = self._status_server(position_response="")
        context = make_context(server, healths={"Alice": 20.0})

        self.assertIsNone(self.manager.system_message(context))

    def test_manager_implements_protocol(self) -> None:
        self.assertIsInstance(BossCombatManager(), TaskManager)
        self.assertIsInstance(BaseTaskManager(), TaskManager)

    def test_base_manager_system_message_is_reserved_hook(self) -> None:
        manager = BaseTaskManager()
        context = make_context(FakeServer())
        self.assertIsNone(manager.system_message(context))


class BossCombatConfigTests(unittest.TestCase):
    def test_configure_rejects_boss_spawn_outside_arena(self) -> None:
        registry = create_default_registry()
        data = registry.get("boss_combat").to_dict()
        data["spawns"][0]["position"] = {"x": 50, "y": 1, "z": 0}
        manager = BossCombatManager()

        with self.assertRaises(TaskSpecError):
            manager.configure(TaskSpec.from_dict(data))

    def test_configure_requires_boss_type(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "boss_combat.missing",
                "category": "boss_combat",
                "name": "Missing Boss Type",
                "agents": {"count": 1},
                "completion": {
                    "timeout_seconds": 60,
                    "success": {"type": "boss_health_below", "ratio": 0.0},
                },
            }
        )
        manager = BossCombatManager()
        with self.assertRaises(TaskSpecError):
            manager.configure(spec)

    def test_configure_rejects_diamond_gear_supply_drop(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "boss_combat.bad_drop",
                "category": "boss_combat",
                "name": "Bad Drop",
                "agents": {"count": 1},
                "spawns": [
                    {"type": "wither", "position": {"x": 35, "y": 1, "z": 0}}
                ],
                "completion": {
                    "timeout_seconds": 60,
                    "success": {"type": "boss_health_below", "ratio": 0.0},
                },
                "params": {
                    "boss_type": "wither",
                    "supply_drop_items": [
                        {"item": "minecraft:diamond_sword", "amount": 1}
                    ],
                },
            }
        )
        manager = BossCombatManager()
        with self.assertRaises(TaskSpecError):
            manager.configure(spec)

    def test_configure_rejects_non_boss_success_type(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "boss_combat.bad",
                "category": "boss_combat",
                "name": "Bad Success",
                "agents": {"count": 1},
                "completion": {
                    "timeout_seconds": 60,
                    "success": {"type": "survive_until", "duration_seconds": 30},
                },
                "params": {"boss_type": "wither"},
            }
        )
        manager = BossCombatManager()
        with self.assertRaises(TaskSpecError):
            manager.configure(spec)


class BaseManagerGenericCompletionTests(unittest.TestCase):
    def test_item_counts_success(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "resource.test",
                "category": "resource_collection",
                "name": "Test",
                "agents": {"count": 2},
                "completion": {
                    "timeout_seconds": 60,
                    "success": {"type": "item_counts", "items": {"diamond": 1}},
                },
            }
        )
        manager = BaseTaskManager()
        manager.configure(spec)
        server = FakeServer()
        context = make_context(
            server,
            inventories={"Alice": {"diamond": 1}},
        )
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.reason, "item_counts")

    def test_survive_until_success(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "survive.test",
                "category": "survive",
                "name": "Test",
                "agents": {"count": 1},
                "completion": {
                    "timeout_seconds": 60,
                    "success": {
                        "type": "survive_until",
                        "duration_seconds": 30,
                    },
                },
            }
        )
        manager = BaseTaskManager()
        manager.configure(spec)
        context = make_context(FakeServer(), elapsed=31.0)
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.reason, "survive_until")

    def test_inventory_list_format_normalized(self) -> None:
        spec = TaskSpec.from_dict(
            {
                "task_id": "resource.test2",
                "category": "resource_collection",
                "name": "Test2",
                "agents": {"count": 1},
                "completion": {
                    "timeout_seconds": 60,
                    "success": {"type": "item_counts", "items": {"bow": 2}},
                },
            }
        )
        manager = BaseTaskManager()
        manager.configure(spec)
        server = FakeServer()

        def perception(agent_id: str):
            return {
                "self": {
                    "inventory": [
                        {"name": "bow", "count": 1},
                        {"name": "bow", "count": 1},
                        {"name": "arrow", "count": 10},
                    ]
                }
            }

        context = EvaluatorContext(
            server=server,
            perception=perception,
            elapsed_seconds=lambda: 0.0,
        )
        outcome = manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
