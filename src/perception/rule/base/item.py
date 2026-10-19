"""
Reusable dropped item rules.
"""

from __future__ import annotations

from .filter import filter_by_contains, filter_by_range
from .select import select_path
from .sort import top_k


def items():
    return select_path("items")


def items_by_name_contains(text: str):
    return filter_by_contains("items", "item.name", text)


def items_by_count(min_count: int | None = None, max_count: int | None = None):
    return filter_by_range("items", "item.count", min_value=min_count, max_value=max_count)


def nearest_items(k: int = 10):
    return top_k("items", k, key="distance")
