"""
Spatial perception rules.
"""

from __future__ import annotations

import math
from typing import Any

from .filter import filter_items
from .utils import distance_between, field_value, get_path, position_tuple


def within_distance(path: str, max_distance: float, *, distance_field: str = "distance"):
    return filter_items(
        path,
        lambda item, context: _distance(item, context, distance_field) is not None
        and _distance(item, context, distance_field) <= max_distance,
    )


def outside_distance(path: str, min_distance: float, *, distance_field: str = "distance"):
    return filter_items(
        path,
        lambda item, context: _distance(item, context, distance_field) is not None
        and _distance(item, context, distance_field) >= min_distance,
    )


def within_box(
    path: str,
    *,
    center_path: str = "self.position",
    half_size: tuple[float, float, float] = (8, 8, 8),
    position_field: str = "position",
):
    def predicate(item, context):
        raw = context.metadata.get("raw_data", {})
        center = position_tuple(get_path(raw, center_path, default=None))
        position = position_tuple(field_value(item, position_field, context))
        if center is None or position is None:
            return False
        return all(abs(pos - cen) <= half for pos, cen, half in zip(position, center, half_size))

    return filter_items(path, predicate)


def in_view(
    path: str,
    *,
    fov_degrees: float = 90,
    self_path: str = "self",
    position_field: str = "position",
):
    def predicate(item, context):
        raw = context.metadata.get("raw_data", {})
        self_state = get_path(raw, self_path, default={})
        origin = position_tuple(self_state.get("position"))
        target = position_tuple(field_value(item, position_field, context))
        if origin is None or target is None:
            return False
        yaw = float(self_state.get("yaw") or 0.0)
        pitch = float(self_state.get("pitch") or 0.0)
        vector = tuple(t - o for t, o in zip(target, origin))
        length = math.sqrt(sum(component * component for component in vector))
        if length <= 0.0001:
            return True
        direction = (
            -math.sin(yaw) * math.cos(pitch),
            math.sin(pitch),
            -math.cos(yaw) * math.cos(pitch),
        )
        dot = sum(d * v for d, v in zip(direction, vector)) / length
        return dot >= math.cos(math.radians(fov_degrees) / 2)

    return filter_items(path, predicate)


def _distance(item: Any, context, distance_field: str) -> float | None:
    value = field_value(item, distance_field, context)
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    raw = context.metadata.get("raw_data", {})
    return distance_between(field_value(item, "position", context), get_path(raw, "self.position", default=None))
