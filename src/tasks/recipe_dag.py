"""可供不同任务复用的配方有向无环图与合成模拟。"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any, Iterable, Mapping, Sequence


class RecipeGraphError(ValueError):
    """配方图定义、需求或库存非法。"""


@dataclass(frozen=True)
class RecipeDefinition:
    """一条与具体任务无关的定量配方。"""

    output: str
    output_count: int
    ingredients: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ItemRequirement:
    """某种物品的需求数量。"""

    item: str
    count: int


@dataclass(frozen=True)
class RecipeCraftStep:
    """按整批执行的一条拓扑合成步骤。"""

    output: str
    batches: int
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    depth: int


@dataclass(frozen=True)
class RecipePlan:
    """不考虑现有库存的保守基础需求与拓扑步骤。"""

    base_requirements: tuple[tuple[str, int], ...]
    steps: tuple[RecipeCraftStep, ...]

    @property
    def base_requirement_map(self) -> dict[str, int]:
        return dict(self.base_requirements)


@dataclass(frozen=True)
class CraftSimulationResult:
    """考虑现有库存和中间产物余量后的合成模拟结果。"""

    feasible: bool
    steps: tuple[RecipeCraftStep, ...]
    missing_items: tuple[tuple[str, int], ...]
    remaining_inventory: tuple[tuple[str, int], ...]

    @property
    def missing_item_map(self) -> dict[str, int]:
        return dict(self.missing_items)

    @property
    def remaining_inventory_map(self) -> dict[str, int]:
        return dict(self.remaining_inventory)


class RecipeDAG:
    """规范化配方、检测环，并提供展开、规划和库存模拟。"""

    def __init__(
        self,
        recipes: Mapping[str, Any],
        base_items: Iterable[str],
    ) -> None:
        if not isinstance(recipes, Mapping) or not recipes:
            raise RecipeGraphError("recipes 必须是非空映射")
        self._base_items = frozenset(_name(item, "base item") for item in base_items)
        if not self._base_items:
            raise RecipeGraphError("base_items 不能为空")
        self._recipes = {
            _name(output, "recipe output"): _normalize_recipe(output, recipe)
            for output, recipe in recipes.items()
        }
        overlap = self._base_items.intersection(self._recipes)
        if overlap:
            raise RecipeGraphError(
                f"物品不能同时是基础材料和配方产物: {sorted(overlap)}"
            )
        for recipe in self._recipes.values():
            for ingredient, _ in recipe.ingredients:
                if ingredient not in self._base_items and ingredient not in self._recipes:
                    raise RecipeGraphError(
                        f"配方材料未定义为基础材料或配方产物: {ingredient}"
                    )
        self._depths = self._calculate_depths()

    @property
    def recipes(self) -> dict[str, RecipeDefinition]:
        return dict(self._recipes)

    @property
    def base_items(self) -> frozenset[str]:
        return self._base_items

    def depths(self) -> dict[str, int]:
        """返回每个配方产物的最长依赖深度；基础材料深度为 0。"""
        return dict(self._depths)

    def topological_outputs(self) -> tuple[str, ...]:
        """按依赖深度和物品名返回稳定的配方产物顺序。"""
        return tuple(
            sorted(self._recipes, key=lambda item: (self._depths[item], item))
        )

    def expand_to_base(
        self,
        requirements: Mapping[str, int] | Sequence[Any],
    ) -> dict[str, int]:
        """把需求按完整批次递归展开为基础材料数量。"""
        normalized = _normalize_requirements(requirements)
        required: dict[str, int] = {}

        def expand(item: str, amount: int) -> None:
            if item in self._base_items:
                required[item] = required.get(item, 0) + amount
                return
            recipe = self._recipes.get(item)
            if recipe is None:
                raise RecipeGraphError(f"需求物品没有配方或基础材料定义: {item}")
            batches = ceil(amount / recipe.output_count)
            for ingredient, count in recipe.ingredients:
                expand(ingredient, count * batches)

        for requirement in normalized:
            expand(requirement.item, requirement.count)
        return required

    def steps_for(
        self,
        requirements: Mapping[str, int] | Sequence[Any],
    ) -> tuple[RecipeCraftStep, ...]:
        """生成不考虑现有库存、依赖优先的保守整批合成步骤。"""
        normalized = _normalize_requirements(requirements)
        batches_by_output: dict[str, int] = {}

        def expand(item: str, amount: int) -> None:
            if item in self._base_items:
                return
            recipe = self._recipes.get(item)
            if recipe is None:
                raise RecipeGraphError(f"需求物品没有配方或基础材料定义: {item}")
            batches = ceil(amount / recipe.output_count)
            batches_by_output[item] = batches_by_output.get(item, 0) + batches
            for ingredient, count in recipe.ingredients:
                expand(ingredient, count * batches)

        for requirement in normalized:
            expand(requirement.item, requirement.count)
        return tuple(
            self._step(output, batches_by_output[output])
            for output in sorted(
                batches_by_output,
                key=lambda item: (self._depths[item], item),
            )
        )

    def plan(
        self,
        requirements: Mapping[str, int] | Sequence[Any],
    ) -> RecipePlan:
        """一次返回基础材料需求和拓扑合成步骤。"""
        base_requirements = self.expand_to_base(requirements)
        return RecipePlan(
            base_requirements=tuple(base_requirements.items()),
            steps=self.steps_for(requirements),
        )

    def simulate(
        self,
        inventory: Mapping[str, int],
        requirements: Mapping[str, int] | Sequence[Any],
    ) -> CraftSimulationResult:
        """模拟逐目标合成；会复用库存中的中间物品和批次余量。"""
        stock = _normalize_inventory(inventory)
        normalized = _normalize_requirements(requirements)
        missing: dict[str, int] = {}
        executed: list[RecipeCraftStep] = []

        def ensure(item: str, amount: int) -> bool:
            available = stock.get(item, 0)
            if available >= amount:
                return True
            recipe = self._recipes.get(item)
            if recipe is None:
                deficit = amount - available
                missing[item] = max(missing.get(item, 0), deficit)
                return False
            missing_amount = amount - available
            batches = ceil(missing_amount / recipe.output_count)
            checkpoint = dict(stock)
            step_count = len(executed)
            for ingredient, count in recipe.ingredients:
                if not ensure(ingredient, count * batches):
                    stock.clear()
                    stock.update(checkpoint)
                    del executed[step_count:]
                    return False
            for ingredient, count in recipe.ingredients:
                stock[ingredient] -= count * batches
            stock[item] = stock.get(item, 0) + recipe.output_count * batches
            executed.append(self._step(item, batches))
            return True

        feasible = True
        for requirement in normalized:
            if not ensure(requirement.item, requirement.count):
                feasible = False
                break
            stock[requirement.item] -= requirement.count
        return CraftSimulationResult(
            feasible=feasible,
            steps=tuple(executed),
            missing_items=tuple(sorted(missing.items())),
            remaining_inventory=tuple(
                sorted((item, count) for item, count in stock.items() if count > 0)
            ),
        )

    def can_produce(
        self,
        inventory: Mapping[str, int],
        requirements: Mapping[str, int] | Sequence[Any],
    ) -> bool:
        return self.simulate(inventory, requirements).feasible

    def _calculate_depths(self) -> dict[str, int]:
        depths: dict[str, int] = {}
        visiting: set[str] = set()

        def visit(item: str) -> int:
            if item in self._base_items:
                return 0
            if item in depths:
                return depths[item]
            if item in visiting:
                raise RecipeGraphError(f"配方 DAG 存在环: {item}")
            visiting.add(item)
            recipe = self._recipes[item]
            depth = 1 + max(visit(ingredient) for ingredient, _ in recipe.ingredients)
            visiting.remove(item)
            depths[item] = depth
            return depth

        for output in self._recipes:
            visit(output)
        return depths

    def _step(self, output: str, batches: int) -> RecipeCraftStep:
        recipe = self._recipes[output]
        return RecipeCraftStep(
            output=output,
            batches=batches,
            output_count=recipe.output_count,
            ingredients=recipe.ingredients,
            depth=self._depths[output],
        )


def _normalize_recipe(output: Any, value: Any) -> RecipeDefinition:
    output_name = _name(output, "recipe output")
    if isinstance(value, Mapping):
        declared_output = value.get("output", output_name)
        output_count = value.get("output_count")
        ingredients = value.get("ingredients")
    else:
        declared_output = getattr(value, "output", output_name)
        output_count = getattr(value, "output_count", None)
        ingredients = getattr(value, "ingredients", None)
    if _name(declared_output, "declared recipe output") != output_name:
        raise RecipeGraphError(f"配方键和 output 不一致: {output_name}")
    if isinstance(output_count, bool) or not isinstance(output_count, int) or output_count < 1:
        raise RecipeGraphError(f"{output_name}.output_count 必须是正整数")
    if isinstance(ingredients, Mapping):
        ingredient_rows = ingredients.items()
    elif isinstance(ingredients, Iterable) and not isinstance(ingredients, (str, bytes)):
        ingredient_rows = ingredients
    else:
        raise RecipeGraphError(f"{output_name}.ingredients 必须是非空映射或键值序列")
    combined: dict[str, int] = {}
    try:
        for ingredient, count in ingredient_rows:
            name = _name(ingredient, f"{output_name} ingredient")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise RecipeGraphError(f"{output_name}.{name} 数量必须是正整数")
            combined[name] = combined.get(name, 0) + count
    except (TypeError, ValueError) as exc:
        if isinstance(exc, RecipeGraphError):
            raise
        raise RecipeGraphError(f"{output_name}.ingredients 不是键值序列") from exc
    if not combined:
        raise RecipeGraphError(f"{output_name}.ingredients 不能为空")
    return RecipeDefinition(output_name, output_count, tuple(combined.items()))


def _normalize_requirements(
    value: Mapping[str, int] | Sequence[Any],
) -> tuple[ItemRequirement, ...]:
    rows = value.items() if isinstance(value, Mapping) else value
    normalized: list[ItemRequirement] = []
    try:
        for row in rows:
            if isinstance(row, ItemRequirement):
                item, count = row.item, row.count
            elif isinstance(row, tuple) and len(row) == 2:
                item, count = row
            else:
                item, count = getattr(row, "item"), getattr(row, "count")
            item_name = _name(item, "requirement item")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise RecipeGraphError(f"{item_name} 需求数量必须是正整数")
            normalized.append(ItemRequirement(item_name, count))
    except (AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, RecipeGraphError):
            raise
        raise RecipeGraphError("requirements 必须是物品数量映射或需求序列") from exc
    if not normalized:
        raise RecipeGraphError("requirements 不能为空")
    return tuple(normalized)


def _normalize_inventory(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RecipeGraphError("inventory 必须是物品数量映射")
    stock: dict[str, int] = {}
    for item, count in value.items():
        item_name = _name(item, "inventory item")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RecipeGraphError(f"{item_name} 库存数量必须是非负整数")
        stock[item_name] = stock.get(item_name, 0) + count
    return stock


def _name(value: Any, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise RecipeGraphError(f"{label} 不能为空")
    return text
