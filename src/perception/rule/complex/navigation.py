"""
Navigation-oriented perception rule groups.
"""

from __future__ import annotations

from src.perception.rule.base import filter_items, pick_fields, select_path, top_k, within_distance
from src.perception.rule.composition import ChainRule, ParallelRule, RuleGroup


NAVIGATION_BLOCK_FIELDS = [
    "name",
    "position",
    "distance",
    "boundingBox",
    "diggable",
    "material",
    "transparent",
]


def nearby_obstacles(k: int = 30, radius: float | None = None) -> RuleGroup:
    """Return nearby non-air, non-transparent blocks useful for obstacle awareness."""
    rules = [
        filter_items("blocks", lambda block, context: not block.get("transparent", False)),
    ]
    if radius is not None:
        rules.append(within_distance("blocks", radius))
    rules.extend([top_k("blocks", k, key="distance"), pick_fields("blocks", NAVIGATION_BLOCK_FIELDS)])
    return RuleGroup("nearby_obstacles", ChainRule(rules), {"k": k, "radius": radius})


def navigation_snapshot(block_k: int = 50, radius: float | None = None) -> RuleGroup:
    """Return full self state and nearby obstacle-oriented block data."""
    return RuleGroup(
        "navigation_snapshot",
        ParallelRule(
            {
                "self": select_path("self"),
                "blocks": nearby_obstacles(k=block_k, radius=radius),
            }
        ),
        {"block_k": block_k, "radius": radius},
    )


def walkable_surface_snapshot(k: int = 30, radius: float | None = None) -> RuleGroup:
    """Return nearby solid blocks as a first-pass walkable surface candidate set."""
    rules = [
        filter_items(
            "blocks",
            lambda block, context: block.get("boundingBox") == "block"
            and not block.get("transparent", False),
        )
    ]
    if radius is not None:
        rules.append(within_distance("blocks", radius))
    rules.extend([top_k("blocks", k, key="distance"), pick_fields("blocks", NAVIGATION_BLOCK_FIELDS)])
    return RuleGroup("walkable_surface_snapshot", ChainRule(rules), {"k": k, "radius": radius})
