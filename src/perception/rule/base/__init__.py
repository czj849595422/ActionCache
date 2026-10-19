"""
Reusable base perception rules.
"""

from .base_rule import BaseRule, RuleContext, RuleLike
from .select import drop_fields, pick_fields, rename_fields, select_path
from .filter import (
    filter_by_contains,
    filter_by_range,
    filter_by_value,
    filter_items,
    filter_not_none,
)
from .sort import sort_by, top_k
from .spatial import in_view, outside_distance, within_box, within_distance
from .entity import (
    entities,
    entities_by_name_contains,
    entities_by_type,
    entities_with_field,
    living_entities,
    nearest_entities,
)
from .item import items, items_by_count, items_by_name_contains, nearest_items
from .block import (
    blocks,
    blocks_by_material,
    blocks_by_name_contains,
    blocks_with_property,
    nearest_blocks,
)

__all__ = [
    "BaseRule",
    "RuleContext",
    "RuleLike",
    "select_path",
    "pick_fields",
    "drop_fields",
    "rename_fields",
    "filter_items",
    "filter_by_value",
    "filter_by_contains",
    "filter_by_range",
    "filter_not_none",
    "sort_by",
    "top_k",
    "within_distance",
    "outside_distance",
    "within_box",
    "in_view",
    "entities",
    "entities_by_type",
    "entities_by_name_contains",
    "entities_with_field",
    "living_entities",
    "nearest_entities",
    "items",
    "items_by_name_contains",
    "items_by_count",
    "nearest_items",
    "blocks",
    "blocks_by_name_contains",
    "blocks_by_material",
    "blocks_with_property",
    "nearest_blocks",
]
