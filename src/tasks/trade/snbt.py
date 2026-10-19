"""Trade-specific item aggregation over the shared SNBT parser."""

from __future__ import annotations

from typing import Any

from src.utils.snbt import (
    SnbtParseError,
    parse_first_snbt_compound,
    parse_first_snbt_list,
    parse_snbt,
)


def parse_chest_items(response: Any) -> dict[str, int]:
    entries = parse_first_snbt_list(response)
    totals: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SnbtParseError("箱子 Items 列表元素必须是 compound")
        item = entry.get("id")
        count = entry.get("Count")
        if not isinstance(item, str) or not isinstance(count, (int, float)):
            raise SnbtParseError("箱子物品缺少 id 或 Count")
        item_id = item if ":" in item else f"minecraft:{item}"
        totals[item_id] = totals.get(item_id, 0) + int(count)
    return totals


__all__ = [
    "SnbtParseError",
    "parse_snbt",
    "parse_first_snbt_list",
    "parse_first_snbt_compound",
    "parse_chest_items",
]
