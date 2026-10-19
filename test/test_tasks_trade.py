#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多阶段合成贸易任务：生成、转换、管理与完成判定测试。"""

from __future__ import annotations

from math import ceil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    EvaluatorContext,
    TaskSpecError,
    TaskStatus,
    TradeTaskManager,
    create_default_registry,
    expand_task_args,
    load_mineflayer_recipe_catalog,
    load_spec,
    skill_agent_options,
)
from src.tasks.trade import (  # noqa: E402
    BASE_PRICES,
    SnbtParseError,
    batch_plans_equal,
    can_craft_targets,
    generate_task_batch,
    generate_trade_config,
    generation_spec_from_template,
    load_command_plan,
    load_generation_template,
    load_trade_config,
    parse_chest_items,
    required_base_materials,
    trade_config_to_prepare_plan,
    find_trade_plans,
)
from src.tasks.spec import task_description_text


TEMPLATE_PATH = ROOT / "src" / "tasks" / "templates" / "trade.quick.yaml"
SPEC_PATH = ROOT / "src" / "tasks" / "specs" / "trade.yaml"


class FakePlanner:
    def __init__(self, config, *, fail: bool = False, extra: bool = False) -> None:
        self.config = config
        self.fail = fail
        self.extra = extra
        self.calls: list[tuple[str, dict]] = []

    def execute_internal(self, action: str, params: dict):
        self.calls.append((action, params))
        if self.fail:
            return SimpleNamespace(ok=False, error="registration failed", result=None)
        chests = []
        for agent in self.config.agents:
            for position, name in (
                (agent.personal_chest, agent.personal_chest_name),
                (agent.shop_chest, agent.shop_chest_name),
            ):
                chests.append(
                    {
                        "id": len(chests) + 1,
                        "name": name,
                        "position": position.to_dict(),
                    }
                )
        if self.extra:
            chests.append(
                {"id": 99, "name": None, "position": {"x": 15, "y": 0, "z": 15}}
            )
        return SimpleNamespace(
            ok=True,
            error=None,
            result={"chests": chests, "chest_count": len(chests)},
        )


class FakeAgent:
    def __init__(self, username: str, planner: FakePlanner) -> None:
        self.username = username
        self.action_planner = planner


class FakeTradeServer:
    def __init__(self, config, *, table_present: bool = True, corrupt: bool = False) -> None:
        self.config = config
        self.is_running = True
        self.table_present = table_present
        self.corrupt = corrupt
        self.commands: list[str] = []
        self.table_score = 0
        self.chests: dict[tuple[int, int, int], dict[int, tuple[str, int]]] = {}
        self.inventories: dict[str, dict[int, tuple[str, int]]] = {
            agent.name: {} for agent in config.agents
        }
        for agent in config.agents:
            self.chests[_pos(agent.personal_chest)] = {}
            for position in agent.shop_blocks:
                self.chests[_pos(position)] = {}

    def send_command(self, command: str, response_timeout: float = 5.0) -> str:
        self.commands.append(command)
        parts = command.split()
        if command.startswith("data merge block "):
            position = (int(parts[3]), int(parts[4]), int(parts[5]))
            if position in self.chests:
                self.chests[position] = {}
            return "Modified block data"
        if command.startswith("clear "):
            self.inventories[command.split()[1]] = {}
            return "Cleared"
        if command.startswith("item replace entity "):
            entity = parts[3]
            slot = int(parts[4].split(".", 1)[1])
            item = parts[6]
            count = int(parts[7])
            stack_size = 16 if item == "minecraft:egg" else 64
            if count > stack_size:
                raise RuntimeError(
                    f"invalid stack count for {item}: {count} > {stack_size}"
                )
            self.inventories[entity][slot] = (item, count)
            return "Replaced item"
        if command.startswith("item replace block "):
            position = (int(parts[3]), int(parts[4]), int(parts[5]))
            slot = int(parts[6].split(".", 1)[1])
            item = parts[8]
            count = int(parts[9])
            stack_size = 16 if item == "minecraft:egg" else 64
            if count > stack_size:
                raise RuntimeError(
                    f"invalid stack count for {item}: {count} > {stack_size}"
                )
            if not (self.corrupt and position == _pos(self.config.agents[0].personal_chest)):
                self.chests[position][slot] = (item, count)
            return "Replaced item"
        if command.startswith("data get block "):
            position = (int(parts[3]), int(parts[4]), int(parts[5]))
            if position not in self.chests:
                return "The target block is not a block entity"
            entries = [
                f'{{Slot:{slot}b,id:"{item}",Count:{count}b}}'
                for slot, (item, count) in sorted(self.chests[position].items())
            ]
            return "Chest has the following block data: [" + ",".join(entries) + "]"
        if command.startswith("data get entity ") and command.endswith(" Inventory"):
            entity = parts[3]
            entries = [
                f'{{Slot:{slot}b,id:"{item}",Count:{count}b}}'
                for slot, (item, count) in sorted(self.inventories[entity].items())
            ]
            return "Entity has the following entity data: [" + ",".join(entries) + "]"
        if command.startswith("scoreboard players set #table acTradeCheck "):
            self.table_score = int(parts[-1])
            return "ok"
        if command.startswith("execute if block 1 0 0 minecraft:crafting_table"):
            if self.table_present:
                self.table_score = 1
            return "ok"
        if command == "scoreboard players get #table acTradeCheck":
            return f"#table has {self.table_score} [acTradeCheck]"
        return "ok"

    def set_items(self, position, stacks: list[tuple[str, int]]) -> None:
        self.chests[_pos(position)] = {
            index: stack for index, stack in enumerate(stacks)
        }


def _pos(position) -> tuple[int, int, int]:
    return (position.x, position.y, position.z)


def make_context(server: FakeTradeServer, elapsed: float) -> EvaluatorContext:
    return EvaluatorContext(
        server=server,
        perception=lambda _: {"self": {"health": 20, "inventory": {}}},
        elapsed_seconds=lambda: elapsed,
    )


class TradeGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = load_generation_template(TEMPLATE_PATH)
        self.generation_spec = generation_spec_from_template(self.template)
        self.item_catalog = load_mineflayer_recipe_catalog()

    def test_seed_is_reproducible_and_different_seed_changes_config(self) -> None:
        first = generate_trade_config(self.generation_spec, 20260809)
        repeated = generate_trade_config(self.generation_spec, 20260809)
        different = generate_trade_config(self.generation_spec, 20260810)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, different)

    def test_recipe_prices_depths_and_ceil_division(self) -> None:
        config = generate_trade_config(self.generation_spec, 20260809)
        self.assertEqual(config.base_price_map["minecraft:gold_ingot"], 9)
        self.assertNotIn("minecraft:coal", config.base_price_map)
        self.assertNotIn("minecraft:redstone", config.base_price_map)
        self.assertEqual(config.price_map["minecraft:bread"], 12)
        self.assertEqual(config.price_map["minecraft:pumpkin_pie"], 10)
        self.assertEqual(config.price_map["minecraft:wooden_axe"], 15)
        self.assertEqual(config.price_map["minecraft:iron_hoe"], 24)
        self.assertEqual(config.price_map["minecraft:golden_sword"], 23)
        self.assertEqual(config.price_map["minecraft:oak_planks"], 2)
        self.assertEqual(config.price_map["minecraft:stick"], 2)
        self.assertNotIn("minecraft:shield", config.target_pool)
        self.assertNotIn("minecraft:iron_helmet", config.target_pool)
        self.assertNotIn("minecraft:piston", config.target_pool)
        self.assertNotIn("minecraft:powered_rail", config.target_pool)
        self.assertTrue(
            {
                "minecraft:wooden_pickaxe",
                "minecraft:wooden_axe",
                "minecraft:stone_pickaxe",
                "minecraft:iron_pickaxe",
                "minecraft:golden_pickaxe",
                "minecraft:iron_sword",
                "minecraft:golden_sword",
                "minecraft:bread",
                "minecraft:pumpkin_pie",
            }.issubset(config.target_pool)
        )
        for target in config.target_pool:
            recipe = config.recipe_map[target]
            self.assertIn(recipe.depth, (1, 2, 3))
        self.assertFalse(config.recipe_map["minecraft:pumpkin_pie"].requires_table)

    def test_generation_enforces_trade_supply_value_and_slots(self) -> None:
        config = generate_trade_config(self.generation_spec, 20260809)
        targets = [target for agent in config.agents for target in agent.targets]
        self.assertEqual(len(targets), 12)
        self.assertEqual(
            {agent.profession for agent in config.agents},
            {"lumberjack", "artisan", "farmer", "knight"},
        )
        all_inventory: dict[str, int] = {}
        other_missing_by_agent = {
            agent.name: {
                item
                for other in config.agents
                if other.name != agent.name
                for item in other.missing_materials
            }
            for agent in config.agents
        }
        for agent in config.agents:
            inventory = agent.initial_inventory
            profession = config.profession_map[agent.profession]
            self.assertTrue(set(inventory).issubset(profession.source_items))
            if agent.profession == "knight":
                self.assertTrue(set(inventory).issubset({"minecraft:gold_ingot"}))
            self.assertEqual(len(agent.targets), 3)
            self.assertNotEqual(agent.targets[0].item, agent.targets[1].item)
            self.assertFalse(can_craft_targets(inventory, agent.targets, config.recipe_map))
            self.assertTrue(all(inventory.get(item, 0) == 0 for item in agent.missing_materials))
            self.assertTrue(any(item in other_missing_by_agent[agent.name] for item in inventory))
            target_value = sum(
                config.price_map[target.item] * target.count for target in agent.targets
            )
            material_value = sum(
                config.price_map[item] * count for item, count in inventory.items()
            )
            self.assertGreaterEqual(agent.initial_currency, target_value)
            self.assertGreater(
                agent.initial_currency + material_value,
                1.5 * target_value,
            )
            slots = ceil(
                agent.initial_currency
                / self.item_catalog.stack_size(config.currency_item)
            ) + sum(
                ceil(count / self.item_catalog.stack_size(item))
                for item, count in inventory.items()
            )
            self.assertLessEqual(slots, 27)
            for item, count in inventory.items():
                all_inventory[item] = all_inventory.get(item, 0) + count
        self.assertTrue(can_craft_targets(all_inventory, targets, config.recipe_map))
        self.assertEqual(config.checks["per_agent"]["Dave"]["private_chest_slots"], 6)
        required = required_base_materials(
            targets, config.recipe_map, set(BASE_PRICES)
        )
        for item, count in required.items():
            self.assertGreaterEqual(all_inventory[item], ceil(count * 1.25))

    def test_batch_generation_writes_config_commands_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundles = generate_task_batch(
                TEMPLATE_PATH,
                count=2,
                base_seed=100,
                output_dir=directory,
            )
            self.assertEqual(len(bundles), 2)
            for bundle in bundles:
                manager = TradeTaskManager()
                manager.configure(load_spec(bundle.task))
                self.assertTrue(bundle.trade_config.is_file())
                self.assertTrue(bundle.commands.is_file())

    def test_runtime_default_spec_configures_without_generated_files(self) -> None:
        spec = expand_task_args(["trade"])[0]
        self.assertEqual(spec.params["generation_seed"], 0)
        self.assertNotIn("trade_config_file", spec.params)
        self.assertNotIn("command_file", spec.params)
        manager = TradeTaskManager()
        manager.configure(spec)
        self.assertIsNotNone(manager._config)
        self.assertEqual(manager._config.generation_seed, 0)
        self.assertTrue(manager._prepare_commands)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = skill_agent_options(spec, root, root / "chests.json")
            config_file = Path(options["bridge_env"]["TRADE_CONFIG_FILE"])
            self.assertEqual(
                config_file.resolve(),
                (root / "trade" / "trade_config.yaml").resolve(),
            )
            self.assertEqual(load_trade_config(config_file), manager._config)

    def test_professions_round_trip_and_trade_prompt_is_minimal(self) -> None:
        config = load_trade_config(ROOT / "src" / "tasks" / "specs" / "resources" / "trade.trade_config.yaml")
        restored = type(config).from_dict(config.to_dict())
        self.assertEqual(restored, config)
        self.assertEqual(
            {agent.profession for agent in restored.agents},
            {"lumberjack", "artisan", "farmer", "knight"},
        )
        spec = load_spec(SPEC_PATH)
        current = next(agent for agent in config.agents if agent.name == "Alice")
        text = task_description_text(spec, agent_id="Alice")
        self.assertIn("名称：Alice", text)
        self.assertIn(f"职业：{config.profession_map[current.profession].label}", text)
        for target in current.targets:
            self.assertIn(target.item.split(":", 1)[-1], text)
        self.assertIn("【其他 Agent 职业】", text)
        self.assertNotIn("source_items", text)
        self.assertNotIn("initial_items", text)
        self.assertNotIn("initial_currency", text)
        self.assertNotIn("gold_nugget", text)
        self.assertNotIn("recipes", text)


class TradeConversionAndSnbtTests(unittest.TestCase):
    def setUp(self) -> None:
        template = load_generation_template(TEMPLATE_PATH)
        self.config = generate_trade_config(
            generation_spec_from_template(template), 20260809
        )

    def test_prepare_commands_put_currency_in_inventory_and_materials_in_private_boxes(self) -> None:
        plan = trade_config_to_prepare_plan(self.config)
        self.assertFalse(plan.task)
        self.assertEqual(sum(command.startswith("data merge block") for command in plan.prepare), 12)
        self.assertEqual(sum(command.startswith("clear Alice") for command in plan.prepare), 1)
        self.assertEqual(sum(command.startswith("clear ") for command in plan.prepare), 4)
        self.assertEqual(sum(command.startswith("tp ") for command in plan.prepare), 4)
        item_commands = [command for command in plan.prepare if command.startswith("item replace")]
        private_positions = {
            f"{agent.personal_chest.x} {agent.personal_chest.y} {agent.personal_chest.z}"
            for agent in self.config.agents
        }
        self.assertTrue(item_commands)
        self.assertTrue(
            all(
                any(f"block {position} " in command for position in private_positions)
                for command in item_commands
                if command.startswith("item replace block")
            )
        )
        currency_commands = [
            command for command in item_commands
            if command.startswith("item replace entity")
        ]
        self.assertEqual(len(currency_commands), len(self.config.agents))
        self.assertTrue(all("inventory.0 with minecraft:gold_nugget" in command for command in currency_commands))
        egg_commands = [command for command in item_commands if "minecraft:egg" in command]
        self.assertEqual(
            egg_commands,
            [
                "item replace block -7 0 1 container.0 with minecraft:egg 16",
                "item replace block -7 0 1 container.1 with minecraft:egg 16",
            ],
        )
        for command in item_commands:
            parts = command.split()
            item_index = 6 if parts[2] == "entity" else 8
            count_index = item_index + 1
            item = parts[item_index]
            count = int(parts[count_index])
            self.assertLessEqual(count, 16 if item == "minecraft:egg" else 64)
        selected_targets = {
            target.item for agent in self.config.agents for target in agent.targets
        }
        self.assertTrue(all(target not in "\n".join(item_commands) for target in selected_targets))

    def test_builtin_command_file_matches_config(self) -> None:
        manager = TradeTaskManager()
        manager.configure(load_spec(SPEC_PATH))
        expected = trade_config_to_prepare_plan(manager._config)
        actual = load_command_plan(
            ROOT / "src" / "tasks" / "specs" / "trade.commands.txt"
        )
        self.assertTrue(batch_plans_equal(expected, actual))

    def test_snbt_parser_sums_duplicate_stacks_and_nested_tags(self) -> None:
        response = (
            'Chest data: [{Slot:0b,id:"minecraft:rail",Count:10b},'
            '{Slot:1b,id:"minecraft:rail",Count:6b,tag:{display:{Name:"x"}}}]'
        )
        self.assertEqual(parse_chest_items(response), {"minecraft:rail": 16})
        with self.assertRaises(SnbtParseError):
            parse_chest_items("no list here")


class TradeSolverTests(unittest.TestCase):
    def setUp(self) -> None:
        template = load_generation_template(TEMPLATE_PATH)
        self.config = generate_trade_config(
            generation_spec_from_template(template), 20260809
        )

    def _config(
        self,
        *,
        second_gold_supplier: bool = False,
        alice_currency: int = 100,
        funding_cycle: bool = False,
    ):
        targets = (
            ("minecraft:golden_hoe", 1),
            ("minecraft:wooden_sword", 1),
            ("minecraft:wooden_sword", 1),
            ("minecraft:wooden_sword", 1),
        )
        inventories = [
            {"minecraft:oak_log": 1},
            {"minecraft:oak_log": 2, "minecraft:gold_ingot": 6},
            {"minecraft:oak_log": 2},
            {"minecraft:oak_log": 2},
        ]
        currencies = [alice_currency, 1, 1, 1]
        if second_gold_supplier:
            inventories[2]["minecraft:gold_ingot"] = 6
        if funding_cycle:
            targets = (
                ("minecraft:golden_hoe", 1),
                ("minecraft:iron_axe", 3),
                ("minecraft:wooden_sword", 1),
                ("minecraft:wooden_sword", 1),
            )
            inventories[0]["minecraft:iron_ingot"] = 9
            inventories[1] = {
                "minecraft:oak_log": 1,
                "minecraft:gold_ingot": 6,
            }
            currencies = [0, 72, 1, 1]
        agents = []
        for index, agent in enumerate(self.config.agents):
            agent_targets = (
                type(agent.targets[0])(targets[index][0], targets[index][1]),
            )
            agents.append(
                replace(
                    agent,
                    targets=agent_targets,
                    crafting_steps=(),
                    initial_currency=currencies[index],
                    initial_items=tuple(sorted(inventories[index].items())),
                    missing_materials=(),
                )
            )
        return replace(self.config, agents=tuple(agents))

    def test_finds_one_plan_and_plan_inventory_is_craftable(self) -> None:
        config = self._config()
        result = find_trade_plans(config)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.plans), 1)
        plan = result.plans[0]
        self.assertEqual(
            [(item.seller, item.buyer, item.item, item.count) for item in plan.transactions],
            [("Bob", "Alice", "minecraft:gold_ingot", 2)],
        )
        inventories = [agent.initial_inventory for agent in config.agents]
        for transaction in plan.transactions:
            seller = next(index for index, agent in enumerate(config.agents) if agent.name == transaction.seller)
            buyer = next(index for index, agent in enumerate(config.agents) if agent.name == transaction.buyer)
            inventories[seller][transaction.item] -= transaction.count
            inventories[buyer][transaction.item] = inventories[buyer].get(transaction.item, 0) + transaction.count
        self.assertTrue(
            all(
                can_craft_targets(inventory, agent.targets, config.recipe_map)
                for inventory, agent in zip(inventories, config.agents)
            )
        )
        self.assertGreaterEqual(len(plan.crafts), 1)

    def test_rejects_missing_supply_and_insufficient_currency(self) -> None:
        missing_supply = self._config()
        missing_supply_agents = tuple(
            replace(
                agent,
                initial_items=tuple(
                    (item, count)
                    for item, count in agent.initial_items
                    if item != "minecraft:gold_ingot"
                ),
            )
            for agent in missing_supply.agents
        )
        self.assertFalse(find_trade_plans(replace(missing_supply, agents=missing_supply_agents)).feasible)
        self.assertFalse(find_trade_plans(self._config(alice_currency=0)).feasible)

    def test_different_sellers_are_different_but_split_and_transfer_are_not(self) -> None:
        result = find_trade_plans(self._config(second_gold_supplier=True))
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.plans), 3)
        for plan in result.plans:
            edges = [(item.seller, item.buyer, item.item) for item in plan.transactions]
            self.assertEqual(len(edges), len(set(edges)))
            self.assertEqual(
                sum(
                    item.count
                    for item in plan.transactions
                    if item.buyer == "Alice" and item.item == "minecraft:gold_ingot"
                ),
                2,
            )
            self.assertTrue(
                all(
                    item.item in agent.initial_inventory
                    for item in plan.transactions
                    for agent in self._config(second_gold_supplier=True).agents
                    if agent.name == item.seller
                )
            )

    def test_currency_dependency_is_reported(self) -> None:
        result = find_trade_plans(self._config(funding_cycle=True))
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.plans), 1)
        dependency_pairs = {(item.before, item.after) for item in result.plans[0].dependencies}
        self.assertIn(
            ("Alice->Bob:minecraft:iron_ingot", "Bob->Alice:minecraft:gold_ingot"),
            dependency_pairs,
        )

    def test_never_returns_more_than_three_plans(self) -> None:
        result = find_trade_plans(self.config, max_solutions=20)
        self.assertLessEqual(len(result.plans), 3)


class TradeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = create_default_registry()
        self.spec = registry.get("trade")
        self.manager = TradeTaskManager()
        self.manager.configure(self.spec)
        self.config = self.manager._config
        self.planner = FakePlanner(self.config)
        self.agents = [
            FakeAgent(("Alice", "Bob", "Carol", "Dave", "Eve")[index], self.planner) for index in range(4)
        ]
        self.server = FakeTradeServer(self.config)

    def tearDown(self) -> None:
        self.manager.teardown()

    def _prepare_and_start(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        self.manager.start()

    def _satisfy_all_targets(self) -> None:
        for agent in self.config.agents:
            stacks: list[tuple[str, int]] = []
            for target in agent.targets:
                if target.count > 1:
                    left = target.count // 2
                    stacks.extend([(target.item, left), (target.item, target.count - left)])
                else:
                    stacks.append((target.item, 1))
            self.server.set_items(agent.personal_chest, stacks)

    def test_prepare_initializes_verifies_and_registers_eight_chests(self) -> None:
        self.manager.prepare(self.server, self.agents, memory=None)
        self.assertEqual(self.planner.calls[0][0], "container.register_chests")
        self.assertEqual(self.planner.calls[0][1]["max_distance"], 16)
        self.assertEqual(len(self.planner.calls[0][1]["names"]), 8)
        self.assertIn("tp Alice 0 0 6", self.server.commands)
        for agent in self.config.agents:
            actual = parse_chest_items(
                self.server.send_command(
                    f"data get block {agent.personal_chest.x} {agent.personal_chest.y} "
                    f"{agent.personal_chest.z} Items"
                )
            )
            expected = agent.initial_inventory
            self.assertEqual(actual, expected)
            inventory = parse_chest_items(
                self.server.send_command(f"data get entity {agent.name} Inventory")
            )
            self.assertEqual(
                inventory,
                {self.config.currency_item: agent.initial_currency},
            )

    def test_fake_server_rejects_overstacked_egg(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "minecraft:egg: 32 > 16"):
            self.server.send_command(
                "item replace block -7 0 1 container.1 with minecraft:egg 32"
            )

    def test_prepare_rejects_missing_table_bad_box_and_registration_failure(self) -> None:
        with self.subTest("table"):
            server = FakeTradeServer(self.config, table_present=False)
            with self.assertRaisesRegex(TaskSpecError, "合成台缺失"):
                self.manager.prepare(server, self.agents, memory=None)

        self.manager.configure(self.spec)
        with self.subTest("box"):
            server = FakeTradeServer(self.config, corrupt=True)
            with self.assertRaisesRegex(TaskSpecError, "初始化内容不一致"):
                self.manager.prepare(server, self.agents, memory=None)

        self.manager.configure(self.spec)
        with self.subTest("registration"):
            planner = FakePlanner(self.config, fail=True)
            agents = [FakeAgent(("Alice", "Bob", "Carol", "Dave", "Eve")[index], planner) for index in range(4)]
            server = FakeTradeServer(self.config)
            with self.assertRaisesRegex(TaskSpecError, "箱子注册失败"):
                self.manager.prepare(server, agents, memory=None)

    def test_all_personal_boxes_satisfied_succeeds_and_latches(self) -> None:
        self._prepare_and_start()
        self._satisfy_all_targets()
        context = make_context(self.server, 100)
        outcome = self.manager.completion_check(context)
        self.assertEqual(outcome.status, TaskStatus.SUCCESS)
        self.assertEqual(outcome.detail["completed_agents"], 4)
        self.server.set_items(self.config.agents[0].personal_chest, [])
        self.assertIs(self.manager.completion_check(context), outcome)

    def test_missing_target_and_query_failure_never_succeed(self) -> None:
        self._prepare_and_start()
        self._satisfy_all_targets()
        first = self.config.agents[0]
        self.server.set_items(first.personal_chest, [])
        running = self.manager.completion_check(make_context(self.server, 100))
        self.assertEqual(running.status, TaskStatus.RUNNING)
        self.assertFalse(running.detail["agents"][first.name]["complete"])

        self.server.chests.pop(_pos(self.config.agents[1].personal_chest))
        running = self.manager.completion_check(make_context(self.server, 101))
        self.assertEqual(running.status, TaskStatus.RUNNING)
        self.assertIn("Bob", running.detail["query_errors"])

    def test_timeout_includes_query_error_and_metrics(self) -> None:
        self._prepare_and_start()
        self.server.chests.pop(_pos(self.config.agents[2].personal_chest))
        running = self.manager.completion_check(make_context(self.server, 299))
        self.assertEqual(running.status, TaskStatus.RUNNING)
        outcome = self.manager.completion_check(make_context(self.server, 300))
        self.assertEqual(outcome.status, TaskStatus.TIMEOUT)
        self.assertIn("Carol", outcome.detail["query_errors"])
        metrics = self.manager.metrics(make_context(self.server, 300))
        self.assertEqual(metrics["total_agents"], 4)
        self.assertIn("target_total_value", metrics["agents"]["Alice"])


if __name__ == "__main__":
    unittest.main()
