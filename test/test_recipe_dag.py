#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共配方 DAG：结构校验、展开、规划和库存模拟测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    ItemRequirement,
    RecipeDAG,
    RecipeDefinition,
    RecipeGraphError,
)


def make_dag() -> RecipeDAG:
    return RecipeDAG(
        {
            "planks": RecipeDefinition("planks", 4, (("log", 1),)),
            "stick": RecipeDefinition("stick", 4, (("planks", 2),)),
            "torch": RecipeDefinition(
                "torch",
                4,
                (("stick", 1), ("coal", 1)),
            ),
        },
        {"log", "coal"},
    )


class RecipeDAGValidationTests(unittest.TestCase):
    def test_depths_and_topological_order_are_stable(self) -> None:
        dag = make_dag()
        self.assertEqual(dag.depths(), {"planks": 1, "stick": 2, "torch": 3})
        self.assertEqual(dag.topological_outputs(), ("planks", "stick", "torch"))

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(RecipeGraphError, "存在环"):
            RecipeDAG(
                {
                    "a": {"output_count": 1, "ingredients": {"b": 1}},
                    "b": {"output_count": 1, "ingredients": {"a": 1}},
                },
                {"raw"},
            )

    def test_undefined_ingredient_and_base_output_overlap_are_rejected(self) -> None:
        with self.assertRaisesRegex(RecipeGraphError, "未定义"):
            RecipeDAG(
                {"a": {"output_count": 1, "ingredients": {"missing": 1}}},
                {"raw"},
            )
        with self.assertRaisesRegex(RecipeGraphError, "同时是"):
            RecipeDAG(
                {"a": {"output_count": 1, "ingredients": {"raw": 1}}},
                {"raw", "a"},
            )

    def test_invalid_quantities_are_rejected(self) -> None:
        with self.assertRaises(RecipeGraphError):
            RecipeDAG(
                {"a": {"output_count": 0, "ingredients": {"raw": 1}}},
                {"raw"},
            )
        with self.assertRaises(RecipeGraphError):
            make_dag().plan({"torch": 0})


class RecipeDAGPlanningTests(unittest.TestCase):
    def test_expand_and_plan_use_whole_batches(self) -> None:
        dag = make_dag()
        plan = dag.plan([ItemRequirement("torch", 5)])
        self.assertEqual(plan.base_requirement_map, {"log": 1, "coal": 2})
        self.assertEqual(
            [(step.output, step.batches) for step in plan.steps],
            [("planks", 1), ("stick", 1), ("torch", 2)],
        )

    def test_static_plan_is_conservative_across_separate_targets(self) -> None:
        dag = make_dag()
        plan = dag.plan({"torch": 4, "stick": 3})
        self.assertEqual(plan.base_requirement_map, {"log": 2, "coal": 1})
        self.assertEqual(
            [(step.output, step.batches) for step in plan.steps],
            [("planks", 2), ("stick", 2), ("torch", 1)],
        )

    def test_simulation_reuses_intermediate_batch_leftovers(self) -> None:
        dag = make_dag()
        result = dag.simulate(
            {"log": 1, "coal": 1},
            {"torch": 4, "stick": 3},
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.missing_item_map, {})
        self.assertEqual(
            [(step.output, step.batches) for step in result.steps],
            [("planks", 1), ("stick", 1), ("torch", 1)],
        )
        self.assertEqual(result.remaining_inventory_map, {"planks": 2})

    def test_simulation_reports_missing_base_without_partial_mutation(self) -> None:
        dag = make_dag()
        result = dag.simulate({"log": 1}, {"torch": 4})
        self.assertFalse(result.feasible)
        self.assertEqual(result.missing_item_map, {"coal": 1})
        self.assertEqual(result.steps, ())
        self.assertEqual(result.remaining_inventory_map, {"log": 1})
        self.assertFalse(dag.can_produce({"log": 1}, {"torch": 4}))

    def test_existing_intermediate_inventory_is_consumed_before_crafting(self) -> None:
        dag = make_dag()
        result = dag.simulate({"stick": 1, "coal": 1}, {"torch": 4})
        self.assertTrue(result.feasible)
        self.assertEqual(
            [(step.output, step.batches) for step in result.steps],
            [("torch", 1)],
        )


if __name__ == "__main__":
    unittest.main()
