"""
Environment summary perception rule groups.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.perception.rule.base import pick_fields, select_path, top_k
from src.perception.rule.composition import ChainRule, ParallelRule, RuleGroup


def count_by_path(source_path: str, field: str, output_path: str, top_n: int | None = None) -> RuleGroup:
    """Count field values from a list path and write them under output_path."""
    output_parts = [part for part in output_path.split(".") if part]

    def rule(data: dict[str, Any], context):
        values = _get_path(data, source_path, [])
        counter = Counter(
            str(_get_path(item, field, None))
            for item in values
            if _get_path(item, field, None) is not None
        )
        items = counter.most_common(top_n)
        return _set_path(output_parts, dict(items))

    return RuleGroup(
        f"count_by_{source_path}_{field}",
        rule,
        {"source_path": source_path, "field": field, "output_path": output_path, "top_n": top_n},
    )


def entity_type_counts(top_n: int | None = None) -> RuleGroup:
    return count_by_path("entities", "type", "summary.entity_type_counts", top_n=top_n)


def block_name_counts(top_n: int | None = None) -> RuleGroup:
    return count_by_path("blocks", "name", "summary.block_name_counts", top_n=top_n)


def item_name_counts(top_n: int | None = None) -> RuleGroup:
    return count_by_path("items", "item.name", "summary.item_name_counts", top_n=top_n)


def environment_counts(top_n: int | None = None) -> RuleGroup:
    """Return entity, item, and block count summaries."""
    return RuleGroup(
        "environment_counts",
        ParallelRule(
            {
                "entities": entity_type_counts(top_n=top_n),
                "items": item_name_counts(top_n=top_n),
                "blocks": block_name_counts(top_n=top_n),
            }
        ),
        {"top_n": top_n},
    )


def nearby_overview(entity_k: int = 10, block_k: int = 20, item_k: int = 10) -> RuleGroup:
    """Return full self state, nearest records, and count summaries."""
    return RuleGroup(
        "nearby_overview",
        ParallelRule(
            {
                "self": select_path("self"),
                "entities": ChainRule(
                    [
                        top_k("entities", entity_k, key="distance"),
                        pick_fields("entities", ["id", "type", "name", "position", "distance", "health"]),
                    ]
                ),
                "items": ChainRule(
                    [
                        top_k("items", item_k, key="distance"),
                        pick_fields("items", ["entityId", "position", "distance", "item.name", "item.count"]),
                    ]
                ),
                "blocks": ChainRule(
                    [
                        top_k("blocks", block_k, key="distance"),
                        pick_fields("blocks", ["name", "position", "distance", "material", "transparent"]),
                    ]
                ),
                "summary": environment_counts(top_n=20),
            }
        ),
        {"entity_k": entity_k, "block_k": block_k, "item_k": item_k},
    )


def _get_path(data: Any, path: str, default: Any = None) -> Any:
    current = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _set_path(parts: list[str], value: Any) -> dict[str, Any]:
    if not parts:
        return value
    root: dict[str, Any] = {}
    current = root
    for part in parts[:-1]:
        current[part] = {}
        current = current[part]
    current[parts[-1]] = value
    return root
