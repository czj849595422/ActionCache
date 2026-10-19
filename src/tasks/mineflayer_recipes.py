"""从本地 Mineflayer 依赖读取并选择版本化 Minecraft 配方。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import subprocess
from types import MappingProxyType
from typing import Iterable, Mapping


DEFAULT_MINECRAFT_VERSION = "1.19"
_BRIDGE_PATH = (
    Path(__file__).resolve().parents[1] / "env" / "mineflayer" / "recipe_catalog.js"
)


class MineflayerRecipeError(ValueError):
    """Mineflayer 配方数据不可用、非法或无法形成目标依赖图。"""


@dataclass(frozen=True)
class MineflayerRecipe:
    """由 Mineflayer 规范化的一条合成配方候选。"""

    output: str
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    requires_table: bool


@dataclass(frozen=True)
class MineflayerRecipeCatalog:
    """一个 Minecraft 版本对应的离线 Mineflayer 配方目录。"""

    provider: str
    minecraft_version: str
    mineflayer_version: str
    minecraft_data_version: str
    item_stack_sizes: Mapping[str, int]
    recipes: Mapping[str, tuple[MineflayerRecipe, ...]]

    def candidates(self, output: str) -> tuple[MineflayerRecipe, ...]:
        return self.recipes.get(_item_id(output), ())

    def stack_size(self, item: str) -> int:
        item_name = _item_id(item)
        try:
            return self.item_stack_sizes[item_name]
        except KeyError as exc:
            raise MineflayerRecipeError(
                f"Mineflayer {self.minecraft_version} 没有物品堆叠数据: {item_name}"
            ) from exc


@lru_cache(maxsize=4)
def load_mineflayer_recipe_catalog(
    minecraft_version: str = DEFAULT_MINECRAFT_VERSION,
) -> MineflayerRecipeCatalog:
    """通过本地 Node 模块加载配方；不连接或启动 Minecraft 服务器。"""
    version = str(minecraft_version).strip()
    if not version:
        raise MineflayerRecipeError("minecraft_version 不能为空")
    if not _BRIDGE_PATH.is_file():
        raise MineflayerRecipeError(f"Mineflayer 配方桥接文件不存在: {_BRIDGE_PATH}")
    try:
        completed = subprocess.run(
            ["node", str(_BRIDGE_PATH), version],
            cwd=_BRIDGE_PATH.parent,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MineflayerRecipeError(f"无法执行 Mineflayer 配方桥接: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise MineflayerRecipeError(f"Mineflayer 配方加载失败: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MineflayerRecipeError("Mineflayer 配方桥接返回了非法 JSON") from exc
    return _parse_catalog(payload, version)


def select_mineflayer_recipes(
    catalog: MineflayerRecipeCatalog,
    outputs: Iterable[str],
    base_items: Iterable[str],
) -> dict[str, MineflayerRecipe]:
    """按 Mineflayer 顺序选择首条能递归归约到基础材料的配方。"""
    targets = tuple(dict.fromkeys(_item_id(item) for item in outputs))
    bases = frozenset(_item_id(item) for item in base_items)
    if not targets:
        raise MineflayerRecipeError("outputs 不能为空")
    if not bases:
        raise MineflayerRecipeError("base_items 不能为空")

    selected: dict[str, MineflayerRecipe] = {}
    visiting: set[str] = set()

    def resolve(item: str) -> None:
        if item in bases or item in selected:
            return
        if item in visiting:
            raise MineflayerRecipeError(f"Mineflayer 配方候选存在依赖环: {item}")
        candidates = catalog.candidates(item)
        if not candidates:
            raise MineflayerRecipeError(
                f"Mineflayer {catalog.minecraft_version} 没有物品配方: {item}"
            )

        visiting.add(item)
        failures: list[str] = []
        try:
            for candidate in candidates:
                checkpoint = dict(selected)
                try:
                    for ingredient, _ in candidate.ingredients:
                        resolve(ingredient)
                except MineflayerRecipeError as exc:
                    selected.clear()
                    selected.update(checkpoint)
                    failures.append(str(exc))
                    continue
                selected[item] = candidate
                return
        finally:
            visiting.discard(item)
        detail = failures[0] if failures else "没有合法候选"
        raise MineflayerRecipeError(f"{item} 无法归约到指定基础材料: {detail}")

    for target in targets:
        resolve(target)
    return selected


def _parse_catalog(payload: object, requested_version: str) -> MineflayerRecipeCatalog:
    if not isinstance(payload, dict):
        raise MineflayerRecipeError("Mineflayer 配方目录顶层必须是映射")
    recipes_value = payload.get("recipes")
    if not isinstance(recipes_value, dict) or not recipes_value:
        raise MineflayerRecipeError("Mineflayer 配方目录缺少 recipes")
    stack_sizes_value = payload.get("item_stack_sizes")
    if not isinstance(stack_sizes_value, dict) or not stack_sizes_value:
        raise MineflayerRecipeError("Mineflayer 配方目录缺少 item_stack_sizes")
    stack_sizes: dict[str, int] = {}
    for item, size in stack_sizes_value.items():
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise MineflayerRecipeError(f"{item} 的堆叠上限非法")
        stack_sizes[_item_id(item)] = size
    parsed: dict[str, tuple[MineflayerRecipe, ...]] = {}
    for output, candidates_value in recipes_value.items():
        output_name = _item_id(output)
        if not isinstance(candidates_value, list) or not candidates_value:
            raise MineflayerRecipeError(f"{output_name} 的配方候选必须是非空列表")
        parsed[output_name] = tuple(
            _parse_recipe(output_name, candidate) for candidate in candidates_value
        )
    actual_version = str(payload.get("minecraft_version") or "").strip()
    if actual_version != requested_version:
        raise MineflayerRecipeError(
            f"Mineflayer 配方版本不一致: 请求 {requested_version}, 返回 {actual_version}"
        )
    return MineflayerRecipeCatalog(
        provider=str(payload.get("provider") or ""),
        minecraft_version=actual_version,
        mineflayer_version=str(payload.get("mineflayer_version") or ""),
        minecraft_data_version=str(payload.get("minecraft_data_version") or ""),
        item_stack_sizes=MappingProxyType(stack_sizes),
        recipes=MappingProxyType(parsed),
    )


def _parse_recipe(output: str, value: object) -> MineflayerRecipe:
    if not isinstance(value, dict):
        raise MineflayerRecipeError(f"{output} 的配方候选必须是映射")
    ingredients_value = value.get("ingredients")
    output_count = value.get("output_count")
    if not isinstance(ingredients_value, dict) or not ingredients_value:
        raise MineflayerRecipeError(f"{output}.ingredients 必须是非空映射")
    if isinstance(output_count, bool) or not isinstance(output_count, int) or output_count < 1:
        raise MineflayerRecipeError(f"{output}.output_count 必须是正整数")
    ingredients: list[tuple[str, int]] = []
    for item, count in ingredients_value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise MineflayerRecipeError(f"{output}.{item} 数量必须是正整数")
        ingredients.append((_item_id(item), count))
    return MineflayerRecipe(
        output=output,
        output_count=output_count,
        ingredients=tuple(ingredients),
        requires_table=bool(value.get("requires_table", False)),
    )


def _item_id(value: object) -> str:
    text = str(value).strip()
    if not text:
        raise MineflayerRecipeError("物品名称不能为空")
    return text if ":" in text else f"minecraft:{text}"
