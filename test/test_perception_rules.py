"""
test_perception_rules.py - perception rule composition semantics
"""

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.perception.rule import ChainRule, ParallelRule, RuleContext, apply_rule


def make_context() -> RuleContext:
    return RuleContext(mode="global", sub_mode="radius", radius=None)


class ParallelRuleAggregationTests(unittest.TestCase):
    def test_merges_same_key_lists_without_branch_wrapping(self):
        rule = ParallelRule(
            {
                "hostiles": lambda data: {"entities": [{"id": 1, "name": "zombie"}]},
                "animals": lambda data: {"entities": [{"id": 2, "name": "cow"}]},
            }
        )

        result = rule.apply({}, make_context())

        self.assertNotIn("hostiles", result)
        self.assertNotIn("animals", result)
        self.assertEqual(
            result["entities"],
            [{"id": 1, "name": "zombie"}, {"id": 2, "name": "cow"}],
        )

    def test_deduplicates_and_merges_same_entity_record(self):
        rule = ParallelRule(
            {
                "position": lambda data: {
                    "entities": [{"id": 1, "name": "zombie", "position": {"x": 1, "y": 2, "z": 3}}]
                },
                "health": lambda data: {"entities": [{"id": 1, "health": 20}]},
            }
        )

        result = rule.apply({}, make_context())

        self.assertEqual(len(result["entities"]), 1)
        self.assertEqual(result["entities"][0]["id"], 1)
        self.assertEqual(result["entities"][0]["name"], "zombie")
        self.assertEqual(result["entities"][0]["health"], 20)

    def test_recursively_merges_dicts_and_overrides_scalar_conflicts(self):
        rule = ParallelRule(
            {
                "a": lambda data: {"self": {"health": 20, "food": 10}, "mode": "a"},
                "b": lambda data: {"self": {"food": 8, "oxygen": 20}, "mode": "b"},
            }
        )

        result = rule.apply({}, make_context())

        self.assertEqual(result["self"], {"health": 20, "food": 8, "oxygen": 20})
        self.assertEqual(result["mode"], "b")

    def test_nested_parallel_rules_keep_aggregate_semantics(self):
        rule = ParallelRule(
            {
                "combat": ParallelRule(
                    {
                        "base": lambda data: {"entities": [{"uuid": "u1", "name": "zombie"}]},
                        "extra": lambda data: {"entities": [{"uuid": "u1", "distance": 4.5}]},
                    }
                ),
                "items": lambda data: {
                    "items": [{"entityId": 3, "item": {"name": "diamond", "count": 3}}]
                },
            }
        )

        result = rule.apply({}, make_context())

        self.assertEqual(result["entities"], [{"uuid": "u1", "name": "zombie", "distance": 4.5}])
        self.assertEqual(result["items"], [{"entityId": 3, "item": {"name": "diamond", "count": 3}}])

    def test_dict_shorthand_uses_aggregate_parallel_rule(self):
        result = apply_rule(
            {
                "a": lambda data: {"entities": [{"id": 1, "name": "zombie"}]},
                "b": lambda data: {"entities": [{"id": 1, "distance": 2}]},
            },
            {},
            make_context(),
        )

        self.assertEqual(result["entities"], [{"id": 1, "name": "zombie", "distance": 2}])

    def test_chain_can_contain_aggregate_parallel_rule(self):
        rule = ChainRule(
            [
                ParallelRule(
                    {
                        "a": lambda data: {"entities": [{"id": 1, "name": "zombie"}]},
                        "b": lambda data: {"entities": [{"id": 1, "health": 20}]},
                    }
                ),
                lambda data: {"count": len(data["entities"]), **data},
            ]
        )

        result = rule.apply({}, make_context())

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["entities"], [{"id": 1, "name": "zombie", "health": 20}])


if __name__ == "__main__":
    unittest.main()
