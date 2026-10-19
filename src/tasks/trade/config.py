"""贸易任务配置、Mineflayer 配方、生成约束与准备指令转换。"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

import yaml

from src.launcher.command_scheduler import BatchPlan, parse_batch_text
from src.tasks.mineflayer_recipes import (
    DEFAULT_MINECRAFT_VERSION,
    MineflayerRecipeError,
    load_mineflayer_recipe_catalog,
    select_mineflayer_recipes,
)
from src.tasks.recipe_dag import RecipeDAG, RecipeGraphError


class TradeConfigError(ValueError):
    """贸易任务配置或生成约束错误。"""


def _item_id(value: str) -> str:
    text = str(value).strip()
    if not text:
        raise TradeConfigError("物品名称不能为空")
    return text if ":" in text else f"minecraft:{text}"


@dataclass(frozen=True)
class BlockPosition:
    x: int
    y: int
    z: int

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "BlockPosition":
        if not isinstance(value, dict):
            raise TradeConfigError(f"{label} 必须是包含 x/y/z 的映射")
        try:
            return cls(int(value["x"]), int(value["y"]), int(value["z"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"{label} 坐标非法: {exc}") from exc

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass(frozen=True)
class TradeRecipe:
    output: str
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    requires_table: bool
    depth: int

    @classmethod
    def from_dict(cls, output: str, value: Any) -> "TradeRecipe":
        if not isinstance(value, dict):
            raise TradeConfigError(f"recipes.{output} 必须是映射")
        ingredients = value.get("ingredients")
        if not isinstance(ingredients, dict) or not ingredients:
            raise TradeConfigError(f"recipes.{output}.ingredients 必须是非空映射")
        try:
            output_count = int(value["output_count"])
            depth = int(value["depth"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"recipes.{output} 数值字段非法: {exc}") from exc
        if output_count < 1 or depth < 1:
            raise TradeConfigError(f"recipes.{output} 的产出数量和深度必须大于 0")
        parsed: list[tuple[str, int]] = []
        for item, count in ingredients.items():
            if not isinstance(count, int) or count < 1:
                raise TradeConfigError(
                    f"recipes.{output}.ingredients.{item} 必须是正整数"
                )
            parsed.append((_item_id(str(item)), count))
        return cls(
            output=_item_id(output),
            output_count=output_count,
            ingredients=tuple(parsed),
            requires_table=bool(value.get("requires_table", False)),
            depth=depth,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_count": self.output_count,
            "ingredients": dict(self.ingredients),
            "requires_table": self.requires_table,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class TradeTarget:
    item: str
    count: int

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "TradeTarget":
        if not isinstance(value, dict):
            raise TradeConfigError(f"{label} 必须是映射")
        try:
            item = _item_id(str(value["item"]))
            count = int(value["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"{label} 非法: {exc}") from exc
        if count < 1:
            raise TradeConfigError(f"{label}.count 必须大于 0")
        return cls(item, count)

    def to_dict(self) -> dict[str, Any]:
        return {"item": self.item, "count": self.count}


@dataclass(frozen=True)
class TradeProfessionConfig:
    """职业定义及其可提供的物资、目标候选。"""

    label: str
    source_items: tuple[str, ...]
    tool_targets: tuple[str, ...]
    food_targets: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "TradeProfessionConfig":
        if not isinstance(value, dict):
            raise TradeConfigError(f"professions.{label} 必须是映射")
        raw_sources = value.get("source_items")
        raw_tools = value.get("tool_targets")
        raw_foods = value.get("food_targets")
        if not isinstance(value.get("label"), str) or not value["label"].strip():
            raise TradeConfigError(f"professions.{label}.label 必须是非空字符串")
        if not all(isinstance(items, list) for items in (raw_sources, raw_tools, raw_foods)):
            raise TradeConfigError(
                f"professions.{label} 的 source_items/tool_targets/food_targets 必须是列表"
            )
        sources = tuple(_item_id(str(item)) for item in raw_sources)
        tools = tuple(_item_id(str(item)) for item in raw_tools)
        foods = tuple(_item_id(str(item)) for item in raw_foods)
        if not sources or len(set(sources)) != len(sources):
            raise TradeConfigError(f"professions.{label}.source_items 必须非空且不重复")
        if len(tools) < 2 or len(set(tools)) != len(tools):
            raise TradeConfigError(
                f"professions.{label}.tool_targets 至少需要 2 个不重复目标"
            )
        if not foods or len(set(foods)) != len(foods):
            raise TradeConfigError(f"professions.{label}.food_targets 必须非空且不重复")
        return cls(value["label"].strip(), sources, tools, foods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source_items": list(self.source_items),
            "tool_targets": list(self.tool_targets),
            "food_targets": list(self.food_targets),
        }


@dataclass(frozen=True)
class CraftingStep:
    output: str
    batches: int
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    depth: int

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "CraftingStep":
        if not isinstance(value, dict):
            raise TradeConfigError(f"{label} 必须是映射")
        ingredients = value.get("ingredients")
        if not isinstance(ingredients, dict) or not ingredients:
            raise TradeConfigError(f"{label}.ingredients 必须是非空映射")
        try:
            return cls(
                output=_item_id(str(value["output"])),
                batches=int(value["batches"]),
                output_count=int(value["output_count"]),
                ingredients=tuple(
                    (_item_id(str(item)), int(count))
                    for item, count in ingredients.items()
                ),
                depth=int(value["depth"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"{label} 非法: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "batches": self.batches,
            "output_count": self.output_count,
            "ingredients": dict(self.ingredients),
            "depth": self.depth,
        }


@dataclass(frozen=True)
class TradeAgentConfig:
    name: str
    profession: str
    spawn: BlockPosition
    personal_chest: BlockPosition
    personal_chest_name: str
    shop_chest: BlockPosition
    shop_chest_name: str
    shop_blocks: tuple[BlockPosition, ...]
    targets: tuple[TradeTarget, ...]
    crafting_steps: tuple[CraftingStep, ...]
    initial_currency: int
    initial_items: tuple[tuple[str, int], ...]
    missing_materials: tuple[str, ...]

    @property
    def initial_inventory(self) -> dict[str, int]:
        return dict(self.initial_items)

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "TradeAgentConfig":
        label = f"agents[{index}]"
        if not isinstance(value, dict):
            raise TradeConfigError(f"{label} 必须是映射")
        personal = value.get("personal_chest")
        shop = value.get("shop")
        if not isinstance(personal, dict) or not isinstance(shop, dict):
            raise TradeConfigError(f"{label} 必须包含 personal_chest 和 shop")
        initial_items = value.get("initial_items")
        if not isinstance(initial_items, dict):
            raise TradeConfigError(f"{label}.initial_items 必须是映射")
        parsed_items: list[tuple[str, int]] = []
        for item, count in initial_items.items():
            if not isinstance(count, int) or count < 1:
                raise TradeConfigError(f"{label}.initial_items.{item} 必须是正整数")
            parsed_items.append((_item_id(str(item)), count))
        targets = value.get("targets")
        steps = value.get("crafting_steps")
        blocks = shop.get("physical_blocks")
        if not isinstance(targets, list) or not isinstance(steps, list):
            raise TradeConfigError(f"{label} 的 targets/crafting_steps 必须是列表")
        if not isinstance(blocks, list):
            raise TradeConfigError(f"{label}.shop.physical_blocks 必须是列表")
        try:
            initial_currency = int(value["initial_currency"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"{label}.initial_currency 非法") from exc
        return cls(
            name=str(value.get("name") or ""),
            profession=str(value.get("profession") or ""),
            spawn=BlockPosition.from_dict(value.get("spawn"), f"{label}.spawn"),
            personal_chest=BlockPosition.from_dict(
                personal.get("position"), f"{label}.personal_chest.position"
            ),
            personal_chest_name=str(personal.get("name") or ""),
            shop_chest=BlockPosition.from_dict(
                shop.get("position"), f"{label}.shop.position"
            ),
            shop_chest_name=str(shop.get("name") or ""),
            shop_blocks=tuple(
                BlockPosition.from_dict(item, f"{label}.shop.physical_blocks[]")
                for item in blocks
            ),
            targets=tuple(
                TradeTarget.from_dict(item, f"{label}.targets[{target_index}]")
                for target_index, item in enumerate(targets)
            ),
            crafting_steps=tuple(
                CraftingStep.from_dict(item, f"{label}.crafting_steps[{step_index}]")
                for step_index, item in enumerate(steps)
            ),
            initial_currency=initial_currency,
            initial_items=tuple(parsed_items),
            missing_materials=tuple(
                _item_id(str(item)) for item in value.get("missing_materials") or []
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "profession": self.profession,
            "spawn": self.spawn.to_dict(),
            "personal_chest": {
                "name": self.personal_chest_name,
                "position": self.personal_chest.to_dict(),
            },
            "shop": {
                "name": self.shop_chest_name,
                "position": self.shop_chest.to_dict(),
                "physical_blocks": [item.to_dict() for item in self.shop_blocks],
            },
            "targets": [target.to_dict() for target in self.targets],
            "crafting_steps": [step.to_dict() for step in self.crafting_steps],
            "initial_currency": self.initial_currency,
            "initial_items": dict(self.initial_items),
            "missing_materials": list(self.missing_materials),
        }


@dataclass(frozen=True)
class TradeTaskConfig:
    version: int
    generation_seed: int
    currency_item: str
    currency_value: int
    crafting_table: BlockPosition
    supply_margin: float
    value_multiplier: float
    base_prices: tuple[tuple[str, int], ...]
    prices: tuple[tuple[str, int], ...]
    recipes: tuple[TradeRecipe, ...]
    target_pool: tuple[str, ...]
    professions: tuple[tuple[str, TradeProfessionConfig], ...]
    agents: tuple[TradeAgentConfig, ...]
    checks: dict[str, Any]

    @property
    def base_price_map(self) -> dict[str, int]:
        return dict(self.base_prices)

    @property
    def price_map(self) -> dict[str, int]:
        return dict(self.prices)

    @property
    def recipe_map(self) -> dict[str, TradeRecipe]:
        return {recipe.output: recipe for recipe in self.recipes}

    @property
    def profession_map(self) -> dict[str, TradeProfessionConfig]:
        return dict(self.professions)

    @classmethod
    def from_dict(cls, value: Any) -> "TradeTaskConfig":
        if not isinstance(value, dict):
            raise TradeConfigError("trade_config 顶层必须是映射")
        currency = value.get("currency")
        constraints = value.get("constraints")
        if not isinstance(currency, dict) or not isinstance(constraints, dict):
            raise TradeConfigError("currency 和 constraints 必须是映射")
        base_prices = _positive_price_map(value.get("base_prices"), "base_prices")
        prices = _positive_price_map(value.get("prices"), "prices")
        recipes_value = value.get("recipes")
        if not isinstance(recipes_value, dict) or not recipes_value:
            raise TradeConfigError("recipes 必须是非空映射")
        recipes = tuple(
            TradeRecipe.from_dict(str(output), recipe)
            for output, recipe in recipes_value.items()
        )
        agents_value = value.get("agents")
        if not isinstance(agents_value, list):
            raise TradeConfigError("agents 必须是列表")
        professions_value = value.get("professions")
        if not isinstance(professions_value, dict) or not professions_value:
            raise TradeConfigError("professions 必须是非空映射")
        professions = tuple(
            (
                str(profession_id),
                TradeProfessionConfig.from_dict(definition, str(profession_id)),
            )
            for profession_id, definition in professions_value.items()
        )
        try:
            config = cls(
                version=int(value["version"]),
                generation_seed=int(value["generation_seed"]),
                currency_item=_item_id(str(currency["item"])),
                currency_value=int(currency["unit_value"]),
                crafting_table=BlockPosition.from_dict(
                    value.get("crafting_table"), "crafting_table"
                ),
                supply_margin=float(constraints["supply_margin"]),
                value_multiplier=float(constraints["value_multiplier"]),
                base_prices=tuple(base_prices.items()),
                prices=tuple(prices.items()),
                recipes=recipes,
                target_pool=tuple(
                    _item_id(str(item)) for item in value.get("target_pool") or []
                ),
                professions=professions,
                agents=tuple(
                    TradeAgentConfig.from_dict(item, index)
                    for index, item in enumerate(agents_value)
                ),
                checks=dict(value.get("checks") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TradeConfigError(f"trade_config 数值字段非法: {exc}") from exc
        validate_trade_config(config)
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation_seed": self.generation_seed,
            "currency": {
                "item": self.currency_item,
                "unit_value": self.currency_value,
            },
            "crafting_table": self.crafting_table.to_dict(),
            "constraints": {
                "supply_margin": self.supply_margin,
                "value_multiplier": self.value_multiplier,
                "private_chest_slots": 27,
            },
            "base_prices": dict(self.base_prices),
            "prices": dict(self.prices),
            "recipes": {recipe.output: recipe.to_dict() for recipe in self.recipes},
            "target_pool": list(self.target_pool),
            "professions": {
                profession_id: profession.to_dict()
                for profession_id, profession in self.professions
            },
            "agents": [agent.to_dict() for agent in self.agents],
            "checks": dict(self.checks),
        }


@dataclass(frozen=True)
class TradeGenerationSpec:
    base_prices: tuple[tuple[str, int], ...]
    target_pool: tuple[str, ...]
    professions: tuple[tuple[str, TradeProfessionConfig], ...] = ()
    supply_margin: float = 0.25
    value_multiplier: float = 1.5
    material_count_min: int = 16
    material_count_max: int = 48
    max_attempts: int = 1000


BASE_PRICES: dict[str, int] = {
    "minecraft:oak_log": 4,
    "minecraft:cobblestone": 2,
    "minecraft:iron_ingot": 8,
    "minecraft:gold_ingot": 9,
    "minecraft:string": 5,
    "minecraft:wheat": 3,
    "minecraft:pumpkin": 3,
    "minecraft:egg": 2,
    "minecraft:sugar": 2,
}


DEFAULT_PROFESSIONS: tuple[tuple[str, TradeProfessionConfig], ...] = (
    (
        "lumberjack",
        TradeProfessionConfig(
            label="伐木工",
            source_items=("minecraft:oak_log",),
            tool_targets=(
                "minecraft:wooden_pickaxe",
                "minecraft:wooden_axe",
                "minecraft:wooden_shovel",
                "minecraft:wooden_hoe",
                "minecraft:wooden_sword",
                "minecraft:bow",
                "minecraft:fishing_rod",
            ),
            food_targets=("minecraft:bread", "minecraft:pumpkin_pie"),
        ),
    ),
    (
        "artisan",
        TradeProfessionConfig(
            label="匠人",
            source_items=(
                "minecraft:cobblestone",
                "minecraft:iron_ingot",
                "minecraft:string",
            ),
            tool_targets=(
                "minecraft:stone_pickaxe",
                "minecraft:stone_axe",
                "minecraft:stone_shovel",
                "minecraft:stone_hoe",
                "minecraft:stone_sword",
                "minecraft:iron_pickaxe",
                "minecraft:iron_axe",
                "minecraft:iron_shovel",
                "minecraft:iron_hoe",
                "minecraft:iron_sword",
                "minecraft:shears",
                "minecraft:bucket",
            ),
            food_targets=("minecraft:bread", "minecraft:pumpkin_pie"),
        ),
    ),
    (
        "farmer",
        TradeProfessionConfig(
            label="农民",
            source_items=(
                "minecraft:wheat",
                "minecraft:pumpkin",
                "minecraft:egg",
                "minecraft:sugar",
            ),
            tool_targets=(
                "minecraft:wooden_hoe",
                "minecraft:stone_hoe",
                "minecraft:iron_hoe",
                "minecraft:golden_hoe",
                "minecraft:wooden_shovel",
                "minecraft:stone_shovel",
                "minecraft:iron_shovel",
                "minecraft:golden_shovel",
                "minecraft:shears",
            ),
            food_targets=("minecraft:bread", "minecraft:pumpkin_pie"),
        ),
    ),
    (
        "knight",
        TradeProfessionConfig(
            label="骑士",
            source_items=("minecraft:gold_ingot",),
            tool_targets=(
                "minecraft:golden_pickaxe",
                "minecraft:golden_axe",
                "minecraft:golden_shovel",
                "minecraft:golden_hoe",
                "minecraft:golden_sword",
                "minecraft:iron_sword",
            ),
            food_targets=("minecraft:bread", "minecraft:pumpkin_pie"),
        ),
    ),
)

PROFESSION_IDS: tuple[str, ...] = tuple(
    profession_id for profession_id, _ in DEFAULT_PROFESSIONS
)


def _profession_target_pool(
    professions: Sequence[tuple[str, TradeProfessionConfig]],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item
            for _, profession in professions
            for item in (*profession.tool_targets, *profession.food_targets)
        )
    )


TARGET_POOL: tuple[str, ...] = _profession_target_pool(DEFAULT_PROFESSIONS)


_FORBIDDEN_RECIPE_BASE_ITEMS = frozenset(
    {"minecraft:coal", "minecraft:redstone"}
)


TRADE_GEOMETRY: tuple[dict[str, Any], ...] = (
    {
        "name": "Alice",
        "spawn": (0, 0, 6),
        "box": (1, 0, 6),
        "shop": (0, 0, 4),
        "shop_blocks": ((0, 0, 4), (1, 0, 4)),
    },
    {
        "name": "Bob",
        "spawn": (0, 0, -6),
        "box": (-1, 0, -6),
        "shop": (-1, 0, -4),
        "shop_blocks": ((-1, 0, -4), (0, 0, -4)),
    },
    {
        "name": "Carol",
        "spawn": (6, 0, 0),
        "box": (6, 0, -1),
        "shop": (4, 0, -1),
        "shop_blocks": ((4, 0, -1), (4, 0, 0)),
    },
    {
        "name": "Dave",
        "spawn": (-6, 0, 0),
        "box": (-7, 0, 1),
        "shop": (-5, 0, 0),
        "shop_blocks": ((-5, 0, 0), (-5, 0, 1)),
    },
)


def fixed_recipes() -> dict[str, TradeRecipe]:
    """从 Mineflayer 1.19 数据构建规范化固定配方 DAG。"""
    try:
        catalog = load_mineflayer_recipe_catalog(DEFAULT_MINECRAFT_VERSION)
        selected = select_mineflayer_recipes(catalog, TARGET_POOL, BASE_PRICES)
    except MineflayerRecipeError as exc:
        raise TradeConfigError(f"无法构建 Mineflayer 配方 DAG: {exc}") from exc
    provisional = {
        output: TradeRecipe(
            output=output,
            output_count=recipe.output_count,
            ingredients=recipe.ingredients,
            requires_table=recipe.requires_table,
            depth=1,
        )
        for output, recipe in selected.items()
    }
    depths = calculate_recipe_depths(provisional, set(BASE_PRICES))
    return {
        output: TradeRecipe(
            output=recipe.output,
            output_count=recipe.output_count,
            ingredients=recipe.ingredients,
            requires_table=recipe.requires_table,
            depth=depths[output],
        )
        for output, recipe in provisional.items()
    }


def calculate_recipe_depths(
    recipes: Mapping[str, TradeRecipe],
    base_items: set[str],
) -> dict[str, int]:
    return _recipe_dag(recipes, base_items).depths()


def calculate_prices(
    base_prices: Mapping[str, int],
    recipes: Mapping[str, TradeRecipe],
) -> dict[str, int]:
    """递归计算单价；多产出配方按整批成本均分并向上取整。"""
    _recipe_dag(recipes, set(base_prices))
    prices = {_item_id(item): int(value) for item, value in base_prices.items()}
    visiting: set[str] = set()

    def price(item: str) -> int:
        if item in prices:
            return prices[item]
        if item in visiting:
            raise TradeConfigError(f"配方价格计算检测到环: {item}")
        recipe = recipes.get(item)
        if recipe is None:
            raise TradeConfigError(f"无法计算未定义物品价格: {item}")
        visiting.add(item)
        material_count = sum(count for _, count in recipe.ingredients)
        batch_cost = sum(price(name) * count for name, count in recipe.ingredients)
        batch_cost += material_count
        visiting.remove(item)
        prices[item] = ceil(batch_cost / recipe.output_count)
        return prices[item]

    for output in recipes:
        price(output)
    return prices


def required_base_materials(
    targets: Sequence[TradeTarget],
    recipes: Mapping[str, TradeRecipe],
    base_items: set[str],
) -> dict[str, int]:
    return _recipe_dag(recipes, base_items).expand_to_base(targets)


def crafting_steps_for_targets(
    targets: Sequence[TradeTarget],
    recipes: Mapping[str, TradeRecipe],
    base_items: set[str],
) -> tuple[CraftingStep, ...]:
    return tuple(
        CraftingStep(
            output=step.output,
            batches=step.batches,
            output_count=step.output_count,
            ingredients=step.ingredients,
            depth=step.depth,
        )
        for step in _recipe_dag(recipes, base_items).steps_for(targets)
    )


def can_craft_targets(
    inventory: Mapping[str, int],
    targets: Sequence[TradeTarget],
    recipes: Mapping[str, TradeRecipe],
) -> bool:
    base_items = {
        ingredient
        for recipe in recipes.values()
        for ingredient, _ in recipe.ingredients
        if ingredient not in recipes
    }
    return _recipe_dag(recipes, base_items).can_produce(inventory, targets)


def _recipe_dag(
    recipes: Mapping[str, TradeRecipe],
    base_items: set[str],
) -> RecipeDAG:
    try:
        return RecipeDAG(recipes, base_items)
    except RecipeGraphError as exc:
        raise TradeConfigError(str(exc)) from exc


def generate_trade_config(
    generation_spec: TradeGenerationSpec,
    seed: int,
) -> TradeTaskConfig:
    """根据 seed 生成满足贸易、供给、价值和箱容量约束的配置。"""
    recipes = fixed_recipes()
    base_prices = dict(generation_spec.base_prices)
    if set(base_prices) != set(BASE_PRICES):
        raise TradeConfigError("生成配置的基础材料集合必须与 Mineflayer 配方一致")
    prices = calculate_prices(base_prices, recipes)
    professions = dict(generation_spec.professions or DEFAULT_PROFESSIONS)
    expected_pool = _profession_target_pool(tuple(professions.items()))
    if tuple(generation_spec.target_pool) != expected_pool:
        raise TradeConfigError("target_pool 必须是四种职业候选目标的并集")
    if set(professions) != set(PROFESSION_IDS):
        raise TradeConfigError("生成配置必须包含四种职业")
    rng = random.Random(seed)
    for _ in range(generation_spec.max_attempts):
        profession_ids = list(PROFESSION_IDS)
        rng.shuffle(profession_ids)
        targets_by_agent: list[tuple[TradeTarget, ...]] = []
        for profession_id in profession_ids:
            profession = professions[profession_id]
            selected_tools = rng.sample(list(profession.tool_targets), 2)
            selected_food = rng.choice(profession.food_targets)
            targets_by_agent.append(
                tuple(
                    TradeTarget(item, recipes[item].output_count)
                    for item in (*selected_tools, selected_food)
                )
            )
        requirements = [
            required_base_materials(targets, recipes, set(base_prices))
            for targets in targets_by_agent
        ]
        allocations = _allocate_materials(
            requirements,
            professions,
            generation_spec,
            rng,
        )
        if allocations is None:
            continue
        agents: list[TradeAgentConfig] = []
        for index, geometry in enumerate(TRADE_GEOMETRY):
            targets = targets_by_agent[index]
            profession_id = profession_ids[index]
            inventory = allocations[profession_id]
            missing = tuple(
                item
                for item in requirements[index]
                if inventory.get(item, 0) == 0
            )
            if not missing:
                break
            target_value = sum(
                prices[target.item] * target.count for target in targets
            )
            material_value = sum(
                prices[item] * count for item, count in inventory.items()
            )
            minimum_total = generation_spec.value_multiplier * target_value
            currency = max(target_value, int(minimum_total - material_value) + 1)
            agent = TradeAgentConfig(
                name=geometry["name"],
                profession=profession_ids[index],
                spawn=BlockPosition(*geometry["spawn"]),
                personal_chest=BlockPosition(*geometry["box"]),
                personal_chest_name=f"{geometry['name']}'s_box",
                shop_chest=BlockPosition(*geometry["shop"]),
                shop_chest_name=f"{geometry['name']}'s_shop",
                shop_blocks=tuple(
                    BlockPosition(*position) for position in geometry["shop_blocks"]
                ),
                targets=targets,
                crafting_steps=crafting_steps_for_targets(
                    targets, recipes, set(base_prices)
                ),
                initial_currency=currency,
                initial_items=tuple(sorted(inventory.items())),
                missing_materials=tuple(sorted(missing)),
            )
            agents.append(agent)
        checks = _build_checks(
            agents,
            recipes,
            base_prices,
            prices,
            generation_spec.supply_margin,
            generation_spec.value_multiplier,
        )
        config = TradeTaskConfig(
            version=1,
            generation_seed=seed,
            currency_item="minecraft:gold_nugget",
            currency_value=1,
            crafting_table=BlockPosition(1, 0, 0),
            supply_margin=generation_spec.supply_margin,
            value_multiplier=generation_spec.value_multiplier,
            base_prices=tuple(base_prices.items()),
            prices=tuple(prices.items()),
            recipes=tuple(recipes.values()),
            target_pool=tuple(generation_spec.target_pool),
            professions=tuple(professions.items()),
            agents=tuple(agents),
            checks=checks,
        )
        try:
            validate_trade_config(config)
        except TradeConfigError:
            continue
        from .solver import find_trade_plans

        verification = find_trade_plans(config)
        if not verification.feasible:
            continue
        return config
    raise TradeConfigError(
        f"在 {generation_spec.max_attempts} 次尝试内无法生成合法贸易配置"
    )


def _allocate_materials(
    requirements: Sequence[dict[str, int]],
    professions: Mapping[str, TradeProfessionConfig],
    spec: TradeGenerationSpec,
    rng: random.Random,
) -> dict[str, dict[str, int]] | None:
    owner_by_item = {
        item: profession_id
        for profession_id, profession in professions.items()
        for item in profession.source_items
    }
    if len(owner_by_item) != len(dict(spec.base_prices)):
        return None
    count_choices = tuple(
        range(spec.material_count_min, spec.material_count_max + 1, 8)
    )
    allocations = {
        profession_id: {
            item: rng.choice(count_choices) for item in profession.source_items
        }
        for profession_id, profession in professions.items()
    }

    total_required: dict[str, int] = {}
    for requirement in requirements:
        for item, count in requirement.items():
            total_required[item] = total_required.get(item, 0) + count
    for item, required in total_required.items():
        target_supply = ceil(required * (1.0 + spec.supply_margin))
        owner = owner_by_item.get(item)
        if owner is None:
            return None
        allocations[owner][item] = max(allocations[owner][item], target_supply)
        if allocations[owner][item] > 64:
            return None
    return allocations


def validate_trade_config(config: TradeTaskConfig) -> None:
    if config.version != 1:
        raise TradeConfigError("trade_config.version 必须为 1")
    if config.currency_item != "minecraft:gold_nugget" or config.currency_value != 1:
        raise TradeConfigError("贸易任务必须使用价值为 1 的 minecraft:gold_nugget")
    if config.crafting_table != BlockPosition(1, 0, 0):
        raise TradeConfigError("合成台必须位于 (1,0,0)")
    if config.supply_margin < 0.25:
        raise TradeConfigError("供给余量不得低于 25%")
    if config.value_multiplier != 1.5:
        raise TradeConfigError("个人总价值约束倍数必须为 1.5")
    if len(config.agents) != 4:
        raise TradeConfigError("贸易任务必须包含 4 名 Agent")
    profession_map = config.profession_map
    if set(profession_map) != set(PROFESSION_IDS):
        raise TradeConfigError("必须定义且仅定义 lumberjack/artisan/farmer/knight 四种职业")
    if profession_map != dict(DEFAULT_PROFESSIONS):
        raise TradeConfigError("职业定义与 Trade 默认职业池不一致")
    source_owner: dict[str, str] = {}
    for profession_id, profession in profession_map.items():
        for item in profession.source_items:
            if item in source_owner:
                raise TradeConfigError(f"基础材料 {item} 被多个职业重复占用")
            source_owner[item] = profession_id
    if set(source_owner) != set(BASE_PRICES):
        raise TradeConfigError("职业来源必须覆盖全部基础材料且不重复")

    base_prices = config.base_price_map
    recipes = config.recipe_map
    if set(base_prices) != set(BASE_PRICES):
        raise TradeConfigError("基础材料集合必须与 Mineflayer 配方 DAG 一致")
    if recipes != fixed_recipes():
        raise TradeConfigError(
            f"recipes 与 Mineflayer {DEFAULT_MINECRAFT_VERSION} 配方数据不一致"
        )
    depths = calculate_recipe_depths(recipes, set(base_prices))
    for output, depth in depths.items():
        if recipes[output].depth != depth:
            raise TradeConfigError(f"配方深度不一致: {output}")
    calculated_prices = calculate_prices(base_prices, recipes)
    if config.price_map != calculated_prices:
        raise TradeConfigError("prices 与递归价格公式不一致")
    expected_target_pool = _profession_target_pool(tuple(profession_map.items()))
    if tuple(config.target_pool) != expected_target_pool:
        raise TradeConfigError("target_pool 必须是职业候选目标的规范并集")
    if not config.target_pool or any(item not in recipes for item in config.target_pool):
        raise TradeConfigError("target_pool 包含未定义配方")
    for target in config.target_pool:
        if _recipe_depends_on_forbidden_base(
            target, recipes, _FORBIDDEN_RECIPE_BASE_ITEMS
        ):
            raise TradeConfigError(
                f"target_pool 包含煤炭或红石相关成品: {target}"
            )

    expected_names = list(("Alice", "Bob", "Carol", "Dave"))
    if [agent.name for agent in config.agents] != expected_names:
        raise TradeConfigError("Agent 名称必须为 Alice 至 Dave")
    agent_professions = [agent.profession for agent in config.agents]
    if set(agent_professions) != set(PROFESSION_IDS) or len(agent_professions) != len(set(agent_professions)):
        raise TradeConfigError("四种职业必须各分配给一个 Agent")
    selected_targets: list[str] = []
    for index, (agent, geometry) in enumerate(zip(config.agents, TRADE_GEOMETRY)):
        _validate_agent_geometry(agent, geometry)
        profession = profession_map.get(agent.profession)
        if profession is None:
            raise TradeConfigError(f"{agent.name}.profession 不存在: {agent.profession}")
        if len(agent.targets) != 3:
            raise TradeConfigError(f"{agent.name} 必须有 2 个工具目标和 1 个食品目标")
        if agent.targets[0].item not in profession.tool_targets or agent.targets[1].item not in profession.tool_targets:
            raise TradeConfigError(f"{agent.name} 的前两个目标必须属于 {agent.profession} 工具候选池")
        if agent.targets[0].item == agent.targets[1].item:
            raise TradeConfigError(f"{agent.name} 的两个工具目标不能重复")
        if agent.targets[2].item not in profession.food_targets:
            raise TradeConfigError(f"{agent.name} 的第三个目标必须属于食品候选池")
        if agent.initial_currency < 1:
            raise TradeConfigError(f"{agent.name} 初始金粒必须大于 0")
        initial = agent.initial_inventory
        if any(item not in profession.source_items for item in initial):
            raise TradeConfigError(f"{agent.name} 初始物资必须来自 {agent.profession} 来源")
        if agent.profession == "knight" and any(
            item != "minecraft:gold_ingot" for item in initial
        ):
            raise TradeConfigError("骑士初始物资只能包含金锭")
        for target in agent.targets:
            selected_targets.append(target.item)
            recipe = recipes.get(target.item)
            if recipe is None or recipe.depth not in (1, 2, 3):
                raise TradeConfigError(f"目标不是 1–3 层可合成成品: {target.item}")
            if target.count % recipe.output_count != 0:
                raise TradeConfigError(f"目标数量不是完整配方批次: {target.item}")
            if target.item in initial:
                raise TradeConfigError(f"初始箱禁止直接提供目标成品: {target.item}")
        expected_steps = crafting_steps_for_targets(
            agent.targets, recipes, set(base_prices)
        )
        if agent.crafting_steps != expected_steps:
            raise TradeConfigError(f"{agent.name} crafting_steps 不是规范拓扑计划")
        required = required_base_materials(agent.targets, recipes, set(base_prices))
        absent = {item for item in required if initial.get(item, 0) == 0}
        if not absent or set(agent.missing_materials) != absent:
            raise TradeConfigError(f"{agent.name} 必须完整记录自身缺少的必需材料")
        if any(item not in absent for item in agent.missing_materials):
            raise TradeConfigError(f"{agent.name}.missing_materials 与库存不一致")
        if can_craft_targets(initial, agent.targets, recipes):
            raise TradeConfigError(f"{agent.name} 可以仅靠个人初始物资完成任务")
        target_value = sum(
            config.price_map[target.item] * target.count for target in agent.targets
        )
        material_value = sum(config.price_map[item] * count for item, count in initial.items())
        if agent.initial_currency < target_value:
            raise TradeConfigError(f"{agent.name} 金粒不足以直接购买目标价值")
        if agent.initial_currency + material_value <= config.value_multiplier * target_value:
            raise TradeConfigError(f"{agent.name} 初始总价值不满足严格 1.5 倍约束")
        slot_count = _inventory_slot_count(
            {config.currency_item: agent.initial_currency, **initial}
        )
        if slot_count > 27:
            raise TradeConfigError(f"{agent.name} 私人箱需要 {slot_count} 槽，超过 27")
        if index >= len(TRADE_GEOMETRY):
            raise TradeConfigError("Agent 几何数量非法")

    if len(selected_targets) != 12:
        raise TradeConfigError("全场必须有 12 个目标成品")
    if any(item not in config.target_pool for item in selected_targets):
        raise TradeConfigError("目标成品不在 target_pool 中")
    initial_target_items = set(selected_targets)
    if any(
        item in initial_target_items
        for agent in config.agents
        for item, _ in agent.initial_items
    ):
        raise TradeConfigError("全场初始库存不得包含任何目标成品")

    all_inventory: dict[str, int] = {}
    all_targets: list[TradeTarget] = []
    for agent in config.agents:
        all_targets.extend(agent.targets)
        for item, count in agent.initial_items:
            all_inventory[item] = all_inventory.get(item, 0) + count
    if not can_craft_targets(all_inventory, all_targets, recipes):
        raise TradeConfigError("全场联合库存无法完成全部目标")

    total_required = required_base_materials(all_targets, recipes, set(base_prices))
    for item, count in total_required.items():
        required_with_margin = ceil(count * (1.0 + config.supply_margin))
        if all_inventory.get(item, 0) < required_with_margin:
            raise TradeConfigError(f"关键材料 {item} 不满足 25% 供给余量")
    for agent in config.agents:
        other_missing = {
            item
            for other in config.agents
            if other.name != agent.name
            for item in other.missing_materials
        }
        if not any(item in other_missing for item, _ in agent.initial_items):
            raise TradeConfigError(f"{agent.name} 未持有其他 Agent 缺少的必需材料")

    expected_checks = _build_checks(
        list(config.agents),
        recipes,
        base_prices,
        config.price_map,
        config.supply_margin,
        config.value_multiplier,
    )
    if config.checks != expected_checks:
        raise TradeConfigError("checks 与实际供给和价值校验结果不一致")


def _recipe_depends_on_forbidden_base(
    output: str,
    recipes: Mapping[str, TradeRecipe],
    forbidden: set[str] | frozenset[str],
) -> bool:
    visiting: set[str] = set()

    def visit(item: str) -> bool:
        if item in forbidden:
            return True
        recipe = recipes.get(item)
        if recipe is None or item in visiting:
            return False
        visiting.add(item)
        try:
            return any(visit(ingredient) for ingredient, _ in recipe.ingredients)
        finally:
            visiting.remove(item)

    return visit(output)


def _validate_agent_geometry(agent: TradeAgentConfig, geometry: dict[str, Any]) -> None:
    expected = (
        BlockPosition(*geometry["spawn"]),
        BlockPosition(*geometry["box"]),
        BlockPosition(*geometry["shop"]),
        tuple(BlockPosition(*item) for item in geometry["shop_blocks"]),
    )
    actual = (agent.spawn, agent.personal_chest, agent.shop_chest, agent.shop_blocks)
    if actual != expected:
        raise TradeConfigError(f"{agent.name} 的出生点或箱子坐标与 trade 存档不一致")
    if agent.personal_chest_name != f"{agent.name}'s_box":
        raise TradeConfigError(f"{agent.name} 私人箱名称非法")
    if agent.shop_chest_name != f"{agent.name}'s_shop":
        raise TradeConfigError(f"{agent.name} 铺子名称非法")
    if len(agent.shop_blocks) != 2:
        raise TradeConfigError(f"{agent.name} 铺子必须是双箱")


def _build_checks(
    agents: Sequence[TradeAgentConfig],
    recipes: Mapping[str, TradeRecipe],
    base_prices: Mapping[str, int],
    prices: Mapping[str, int],
    supply_margin: float,
    value_multiplier: float,
) -> dict[str, Any]:
    all_targets = [target for agent in agents for target in agent.targets]
    required = required_base_materials(all_targets, recipes, set(base_prices))
    supply: dict[str, int] = {}
    per_agent: dict[str, Any] = {}
    for agent in agents:
        inventory = agent.initial_inventory
        for item, count in inventory.items():
            supply[item] = supply.get(item, 0) + count
        target_value = sum(prices[target.item] * target.count for target in agent.targets)
        material_value = sum(prices[item] * count for item, count in inventory.items())
        per_agent[agent.name] = {
            "target_value": target_value,
            "initial_material_value": material_value,
            "initial_currency": agent.initial_currency,
            "initial_total_value": material_value + agent.initial_currency,
            "required_value_floor": value_multiplier * target_value,
            "individual_can_complete": can_craft_targets(
                inventory, agent.targets, recipes
            ),
            "private_chest_slots": _inventory_slot_count(
                {"minecraft:gold_nugget": agent.initial_currency, **inventory}
            ),
        }
    return {
        "per_agent": per_agent,
        "global_required_base": required,
        "global_base_supply": {item: supply.get(item, 0) for item in required},
        "global_required_with_margin": {
            item: ceil(count * (1.0 + supply_margin))
            for item, count in required.items()
        },
        "global_can_complete": can_craft_targets(supply, all_targets, recipes),
        "all_agents_require_trade": all(
            not can_craft_targets(agent.initial_inventory, agent.targets, recipes)
            for agent in agents
        ),
    }


def _positive_price_map(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise TradeConfigError(f"{label} 必须是非空映射")
    result: dict[str, int] = {}
    for item, price in value.items():
        if not isinstance(price, int) or price < 1:
            raise TradeConfigError(f"{label}.{item} 必须是正整数")
        result[_item_id(str(item))] = price
    return result


def _inventory_slot_count(items: Mapping[str, int]) -> int:
    return sum(
        ceil(int(count) / _item_stack_size(item))
        for item, count in items.items()
        if int(count) > 0
    )


def _item_stack_size(item: str) -> int:
    try:
        return load_mineflayer_recipe_catalog(DEFAULT_MINECRAFT_VERSION).stack_size(item)
    except MineflayerRecipeError as exc:
        raise TradeConfigError(f"无法读取 {item} 的 Minecraft 堆叠上限: {exc}") from exc


def load_trade_config(path: str | Path) -> TradeTaskConfig:
    target = Path(path)
    try:
        value = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TradeConfigError(f"无法解析贸易配置 {target}: {exc}") from exc
    config = TradeTaskConfig.from_dict(value)
    from .solver import find_trade_plans

    verification = find_trade_plans(config)
    if not verification.feasible:
        reasons = "; ".join(verification.failure_reasons)
        raise TradeConfigError(f"trade_config 不存在可执行解: {reasons}")
    return config


def write_trade_config(config: TradeTaskConfig, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return target


def trade_config_to_prepare_plan(config: TradeTaskConfig) -> BatchPlan:
    commands: list[str] = []
    physical_chests: list[BlockPosition] = []
    for agent in config.agents:
        physical_chests.append(agent.personal_chest)
        physical_chests.extend(agent.shop_blocks)
    for position in physical_chests:
        commands.append(
            f"data merge block {position.x} {position.y} {position.z} {{Items:[]}}"
        )
    for agent in config.agents:
        commands.append(f"clear {agent.name}")
        commands.append(
            f"tp {agent.name} {agent.spawn.x} {agent.spawn.y} {agent.spawn.z}"
        )
    for agent in config.agents:
        slot = 0
        remaining = agent.initial_currency
        while remaining > 0:
            count = min(_item_stack_size(config.currency_item), remaining)
            commands.append(
                f"item replace entity {agent.name} inventory.{slot} "
                f"with {config.currency_item} {count}"
            )
            slot += 1
            remaining -= count

        slot = 0
        for item, total in agent.initial_items:
            remaining = total
            while remaining > 0:
                count = min(_item_stack_size(item), remaining)
                commands.append(
                    f"item replace block {agent.personal_chest.x} "
                    f"{agent.personal_chest.y} {agent.personal_chest.z} "
                    f"container.{slot} with {item} {count}"
                )
                slot += 1
                remaining -= count
    return BatchPlan(prepare=commands)


def command_file_text(config: TradeTaskConfig) -> str:
    plan = trade_config_to_prepare_plan(config)
    lines = ["# Generated trade preparation commands", "[prepare]"]
    lines.extend(plan.prepare)
    return "\n".join(lines) + "\n"


def write_command_file(config: TradeTaskConfig, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(command_file_text(config), encoding="utf-8")
    return target


def batch_plans_equal(left: BatchPlan, right: BatchPlan) -> bool:
    return left.prepare == right.prepare and [
        (entry.time, entry.unit, entry.command) for entry in left.task
    ] == [
        (entry.time, entry.unit, entry.command) for entry in right.task
    ]


def load_command_plan(path: str | Path) -> BatchPlan:
    try:
        return parse_batch_text(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TradeConfigError(f"无法解析贸易指令文件 {path}: {exc}") from exc
