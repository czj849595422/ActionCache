#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mineflayer 配方目录加载与 DAG 依赖选择测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    DEFAULT_MINECRAFT_VERSION,
    MineflayerRecipeError,
    RecipeDAG,
    load_mineflayer_recipe_catalog,
    select_mineflayer_recipes,
)
from src.tasks.trade import BASE_PRICES, TARGET_POOL, fixed_recipes  # noqa: E402


class MineflayerRecipeCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_mineflayer_recipe_catalog()

    def test_catalog_comes_from_installed_mineflayer_minecraft_data(self) -> None:
        self.assertEqual(self.catalog.provider, "mineflayer/minecraft-data")
        self.assertEqual(self.catalog.minecraft_version, DEFAULT_MINECRAFT_VERSION)
        self.assertTrue(self.catalog.mineflayer_version)
        self.assertTrue(self.catalog.minecraft_data_version)
        self.assertGreater(len(self.catalog.recipes), 600)

    def test_item_stack_sizes_come_from_minecraft_data(self) -> None:
        self.assertEqual(self.catalog.stack_size("minecraft:egg"), 16)
        self.assertEqual(self.catalog.stack_size("minecraft:gold_nugget"), 64)
        with self.assertRaisesRegex(MineflayerRecipeError, "没有物品堆叠数据"):
            self.catalog.stack_size("minecraft:not_an_item")

    def test_known_recipes_preserve_materials_output_count_and_table_flag(self) -> None:
        hopper = self.catalog.candidates("minecraft:hopper")[0]
        self.assertEqual(
            dict(hopper.ingredients),
            {"minecraft:iron_ingot": 5, "minecraft:chest": 1},
        )
        self.assertEqual(hopper.output_count, 1)
        self.assertTrue(hopper.requires_table)

        ladder = self.catalog.candidates("ladder")[0]
        self.assertEqual(dict(ladder.ingredients), {"minecraft:stick": 7})
        self.assertEqual(ladder.output_count, 3)
        self.assertTrue(ladder.requires_table)

        expected = {
            "minecraft:iron_helmet": {"minecraft:iron_ingot": 5},
            "minecraft:iron_chestplate": {"minecraft:iron_ingot": 8},
            "minecraft:iron_leggings": {"minecraft:iron_ingot": 7},
            "minecraft:iron_boots": {"minecraft:iron_ingot": 4},
            "minecraft:iron_sword": {
                "minecraft:iron_ingot": 2,
                "minecraft:stick": 1,
            },
            "minecraft:golden_helmet": {"minecraft:gold_ingot": 5},
            "minecraft:golden_chestplate": {"minecraft:gold_ingot": 8},
            "minecraft:golden_leggings": {"minecraft:gold_ingot": 7},
            "minecraft:golden_boots": {"minecraft:gold_ingot": 4},
            "minecraft:golden_sword": {
                "minecraft:gold_ingot": 2,
                "minecraft:stick": 1,
            },
            "minecraft:bread": {"minecraft:wheat": 3},
            "minecraft:pumpkin_pie": {
                "minecraft:pumpkin": 1,
                "minecraft:sugar": 1,
                "minecraft:egg": 1,
            },
        }
        for output, ingredients in expected.items():
            recipe = self.catalog.candidates(output)[0]
            self.assertEqual(dict(recipe.ingredients), ingredients)
            self.assertEqual(recipe.output_count, 1)
            self.assertEqual(recipe.requires_table, output != "minecraft:pumpkin_pie")

    def test_selection_builds_dependency_closure_from_mineflayer_candidates(self) -> None:
        selected = select_mineflayer_recipes(
            self.catalog,
            ["minecraft:hopper", "minecraft:ladder"],
            ["minecraft:oak_log", "minecraft:iron_ingot"],
        )
        self.assertEqual(
            set(selected),
            {
                "minecraft:oak_planks",
                "minecraft:stick",
                "minecraft:chest",
                "minecraft:hopper",
                "minecraft:ladder",
            },
        )
        dag = RecipeDAG(selected, {"minecraft:oak_log", "minecraft:iron_ingot"})
        self.assertEqual(dag.depths()["minecraft:hopper"], 3)
        self.assertEqual(dag.depths()["minecraft:ladder"], 3)

    def test_selection_rejects_targets_that_cannot_reach_base_items(self) -> None:
        with self.assertRaisesRegex(MineflayerRecipeError, "无法归约"):
            select_mineflayer_recipes(
                self.catalog,
                ["minecraft:hopper"],
                ["minecraft:cobblestone"],
            )

    def test_trade_recipe_snapshot_is_derived_from_mineflayer(self) -> None:
        selected = select_mineflayer_recipes(
            self.catalog,
            TARGET_POOL,
            BASE_PRICES,
        )
        trade_recipes = fixed_recipes()
        self.assertEqual(set(trade_recipes), set(selected))
        for output, recipe in trade_recipes.items():
            source = selected[output]
            self.assertEqual(recipe.output_count, source.output_count)
            self.assertEqual(recipe.ingredients, source.ingredients)
            self.assertEqual(recipe.requires_table, source.requires_table)


if __name__ == "__main__":
    unittest.main()
