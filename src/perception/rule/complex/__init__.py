"""
Complex reusable perception rule sets.

This package is reserved for task-oriented rule groups composed from base rules.
"""

from .combat import combat_chest_snapshot, combat_snapshot, nearby_chests, nearest_hostiles, threat_summary
from .environment import (
    block_name_counts,
    entity_type_counts,
    environment_counts,
    item_name_counts,
    nearby_overview,
)
from .navigation import navigation_snapshot, nearby_obstacles, walkable_surface_snapshot
from .pickup import nearest_pickups, pickup_snapshot, valuable_pickups

__all__ = [
    "combat_snapshot",
    "combat_chest_snapshot",
    "nearest_hostiles",
    "nearby_chests",
    "threat_summary",
    "nearest_pickups",
    "pickup_snapshot",
    "valuable_pickups",
    "navigation_snapshot",
    "nearby_obstacles",
    "walkable_surface_snapshot",
    "entity_type_counts",
    "block_name_counts",
    "item_name_counts",
    "environment_counts",
    "nearby_overview",
]
