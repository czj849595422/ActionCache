"""
Reusable entity rules.
"""

from __future__ import annotations

from .filter import filter_by_contains, filter_by_value, filter_not_none
from .select import select_path
from .sort import top_k


def entities():
    return select_path("entities")


def entities_by_type(type_name: str):
    return filter_by_value("entities", "type", type_name)


def entities_by_name_contains(text: str):
    return filter_by_contains("entities", "name", text)


def entities_with_field(field: str):
    return filter_not_none("entities", field)


def living_entities():
    return filter_not_none("entities", "health")


def nearest_entities(k: int = 10):
    return top_k("entities", k, key="distance")
