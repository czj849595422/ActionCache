"""
Composable perception rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from .base.base_rule import RuleContext, RuleLike
from .engine import apply_rule


@dataclass
class ChainRule:
    """Run rules in sequence: raw -> rule1 -> rule2 -> result."""

    rules: list[RuleLike]
    name: str = "chain"

    def apply(self, data: Any, context: RuleContext) -> Any:
        result = data
        for rule in self.rules:
            result = apply_rule(rule, result, context)
        return result


@dataclass
class ParallelRule:
    """Run rules against the same input and aggregate their outputs."""

    branches: dict[str, RuleLike]
    name: str = "parallel"

    def apply(self, data: Any, context: RuleContext) -> Any:
        result = _MISSING
        for rule in self.branches.values():
            branch_result = apply_rule(rule, data, context)
            result = _aggregate_values(result, branch_result)
        return {} if result is _MISSING else result


@dataclass
class RuleGroup:
    """Named reusable rule composition."""

    name: str
    rule: RuleLike
    metadata: dict[str, Any] = field(default_factory=dict)

    def apply(self, data: Any, context: RuleContext) -> Any:
        context.metadata.setdefault("rule_groups", []).append(
            {"name": self.name, "metadata": self.metadata}
        )
        return apply_rule(self.rule, data, context)


_MISSING = object()


def _aggregate_values(left: Any, right: Any) -> Any:
    if left is _MISSING:
        return right
    if isinstance(left, dict) and isinstance(right, dict):
        return _merge_dicts(left, right)
    if isinstance(left, list) or isinstance(right, list):
        left_items = left if isinstance(left, list) else [left]
        right_items = right if isinstance(right, list) else [right]
        return _dedupe_list([*left_items, *right_items])
    return right


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged:
            merged[key] = _aggregate_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dedupe_list(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    index_by_identity: dict[tuple[Any, ...], int] = {}

    for item in items:
        identity = _item_identity(item)
        if identity not in index_by_identity:
            index_by_identity[identity] = len(result)
            result.append(item)
            continue

        existing_index = index_by_identity[identity]
        existing = result[existing_index]
        if isinstance(existing, dict) and isinstance(item, dict):
            result[existing_index] = _merge_dicts(existing, item)

    return result


def _item_identity(item: Any) -> tuple[Any, ...]:
    if isinstance(item, dict):
        for key in ("uuid", "entityId", "id"):
            value = item.get(key)
            if value is not None:
                return (key, str(value))

        position = _position_identity(item.get("position"))
        name = item.get("name") or item.get("displayName") or item.get("entityName")
        if name is not None and position is not None:
            return ("name_position", str(name), position)

        type_ = item.get("type") or item.get("entityType")
        if type_ is not None and position is not None:
            return ("type_position", str(type_), position)

    return ("json", _stable_json(item))


def _position_identity(position: Any) -> tuple[float, float, float] | None:
    if not isinstance(position, dict):
        return None
    try:
        return (
            round(float(position["x"]), 3),
            round(float(position["y"]), 3),
            round(float(position["z"]), 3),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
