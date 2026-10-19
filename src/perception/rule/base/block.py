"""
Reusable block rules.
"""

from __future__ import annotations

from .filter import filter_by_contains, filter_by_value, filter_items
from .select import select_path
from .sort import top_k
from .utils import get_path


def blocks():
    return select_path("blocks")


def blocks_by_name_contains(text: str):
    return filter_by_contains("blocks", "name", text)


def blocks_by_material(material: str):
    return filter_by_value("blocks", "material", material)


def blocks_with_property(key: str, value=None):
    def predicate(block, context):
        properties = block.get("properties", {}) if isinstance(block, dict) else {}
        actual = get_path(properties, key, default=None)
        if value is None:
            return actual is not None
        return actual == value

    return filter_items("blocks", predicate)


def nearest_blocks(k: int = 20):
    return top_k("blocks", k, key="distance")
