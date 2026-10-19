"""
Dropped-item pickup perception rule groups.
"""

from __future__ import annotations

from src.perception.rule.base import filter_items, pick_fields, select_path, top_k, within_distance
from src.perception.rule.composition import ChainRule, ParallelRule, RuleGroup


def nearest_pickups(k: int = 10, radius: float | None = None) -> RuleGroup:
    """Return nearest dropped item views."""
    rules = []
    if radius is not None:
        rules.append(within_distance("items", radius))
    rules.extend(
        [
            top_k("items", k, key="distance"),
            pick_fields(
                "items",
                ["entityId", "uuid", "position", "distance", "item.name", "item.displayName", "item.count"],
            ),
        ]
    )
    return RuleGroup("nearest_pickups", ChainRule(rules), {"k": k, "radius": radius})


def valuable_pickups(
    names: list[str] | tuple[str, ...] | None = None,
    k: int = 10,
    radius: float | None = None,
) -> RuleGroup:
    """Return nearest dropped items whose names contain any provided token."""
    lowered = [name.lower() for name in names] if names else []
    rules = []
    if lowered:
        rules.append(
            filter_items(
                "items",
                lambda item, context: any(
                    token in str(item.get("item", {}).get("name", "")).lower()
                    for token in lowered
                ),
            )
        )
    if radius is not None:
        rules.append(within_distance("items", radius))
    rules.extend(
        [
            top_k("items", k, key="distance"),
            pick_fields(
                "items",
                ["entityId", "uuid", "position", "distance", "item.name", "item.displayName", "item.count"],
            ),
        ]
    )
    return RuleGroup("valuable_pickups", ChainRule(rules), {"names": list(names or []), "k": k, "radius": radius})


def pickup_snapshot(k: int = 10, radius: float | None = None) -> RuleGroup:
    """Return full self state and nearest dropped items."""
    return RuleGroup(
        "pickup_snapshot",
        ParallelRule(
            {
                "self": select_path("self"),
                "items": nearest_pickups(k=k, radius=radius),
            }
        ),
        {"k": k, "radius": radius},
    )
