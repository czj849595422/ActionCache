"""
Combat-oriented perception rule groups.
"""

from __future__ import annotations

from src.perception.constants import DEFAULT_OBSERVATION_RANGE
from src.perception.rule.base import (
    blocks_by_name_contains,
    entities_by_type,
    pick_fields,
    select_path,
    top_k,
    within_distance,
)
from src.perception.rule.composition import ChainRule, ParallelRule, RuleGroup


def nearest_hostiles(k: int = 8, radius: float | None = None) -> RuleGroup:
    """Return nearest hostile entities with compact combat fields."""
    rules = [entities_by_type("hostile")]
    if radius is not None:
        rules.append(within_distance("entities", radius))
    rules.extend(
        [
            top_k("entities", k, key="distance"),
            pick_fields(
                "entities",
                ["id", "uuid", "type", "name", "position", "distance", "health", "yaw", "pitch"],
            ),
        ]
    )
    return RuleGroup("nearest_hostiles", ChainRule(rules), {"k": k, "radius": radius})


def combat_snapshot(k: int = 8, radius: float | None = None) -> RuleGroup:
    """Return full self state and nearest hostiles."""
    return RuleGroup(
        "combat_snapshot",
        ParallelRule(
            {
                "self": select_path("self"),
                "hostiles": nearest_hostiles(k=k, radius=radius),
            }
        ),
        {"k": k, "radius": radius},
    )


def nearby_chests(k: int = 8, radius: float | None = None) -> RuleGroup:
    """Return nearby chest blocks with compact fields."""
    rules = [blocks_by_name_contains("chest")]
    if radius is not None:
        rules.append(within_distance("blocks", radius))
    rules.extend(
        [
            top_k("blocks", k, key="distance"),
            pick_fields("blocks", ["name", "position", "distance", "material", "transparent"]),
        ]
    )
    return RuleGroup("nearby_chests", ChainRule(rules), {"k": k, "radius": radius})


def combat_chest_snapshot(
    k: int = 8,
    chest_k: int = 8,
    radius: float | None = DEFAULT_OBSERVATION_RANGE,
) -> RuleGroup:
    """Return self state, nearby hostiles, and nearby chests."""
    return RuleGroup(
        "combat_chest_snapshot",
        ParallelRule(
            {
                "self": select_path("self"),
                "hostiles": nearest_hostiles(k=k, radius=radius),
                "chests": nearby_chests(k=chest_k, radius=radius),
            }
        ),
        {"k": k, "chest_k": chest_k, "radius": radius},
    )


def threat_summary(k: int = 8, radius: float | None = None):
    """Return a compact threat summary dict."""
    hostiles_rule = nearest_hostiles(k=k, radius=radius)

    def summarize(data, context):
        result = hostiles_rule.apply(data, context)
        hostiles = result.get("entities", [])
        nearest = hostiles[0]["distance"] if hostiles else None
        return {
            "summary": {
                "hostile_count": len(hostiles),
                "nearest_hostile_distance": nearest,
            }
        }

    return RuleGroup("threat_summary", summarize, {"k": k, "radius": radius})
