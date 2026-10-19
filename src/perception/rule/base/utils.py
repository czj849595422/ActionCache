"""
Utilities shared by base perception rules.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Callable, Iterable

from .base_rule import RuleContext


MISSING = object()


def split_path(path: str | Iterable[str]) -> list[str]:
    if isinstance(path, str):
        return [part for part in path.split(".") if part]
    return [str(part) for part in path]


def get_path(data: Any, path: str | Iterable[str], default: Any = MISSING) -> Any:
    current = data
    for part in split_path(path):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if default is MISSING:
            raise KeyError(".".join(split_path(path)))
        return default
    return current


def set_path(path: str | Iterable[str], value: Any) -> dict[str, Any]:
    parts = split_path(path)
    if not parts:
        return value
    root: dict[str, Any] = {}
    current = root
    for part in parts[:-1]:
        current[part] = {}
        current = current[part]
    current[parts[-1]] = value
    return root


def apply_at_path(
    path: str | Iterable[str],
    transform: Callable[[Any, RuleContext], Any],
) -> Callable[[dict[str, Any], RuleContext], dict[str, Any]]:
    def rule(data: dict[str, Any], context: RuleContext) -> dict[str, Any]:
        value = get_path(data, path, default=MISSING)
        if value is MISSING:
            return {}
        return set_path(path, transform(value, context))

    return rule


def field_value(item: Any, field: str | Callable[..., Any], context: RuleContext | None = None) -> Any:
    if callable(field):
        try:
            return field(item, context)
        except TypeError:
            return field(item)
    if isinstance(item, dict):
        return get_path(item, field, default=None)
    return getattr(item, field, None)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def copy_selected_fields(item: Any, fields: list[str]) -> Any:
    if not isinstance(item, dict):
        return item
    selected: dict[str, Any] = {}
    for field in fields:
        value = get_path(item, field, default=MISSING)
        if value is not MISSING:
            _set_nested(selected, split_path(field), deepcopy(value))
    return selected


def drop_selected_fields(item: Any, fields: list[str]) -> Any:
    if not isinstance(item, dict):
        return item
    result = deepcopy(item)
    for field in fields:
        _delete_nested(result, split_path(field))
    return result


def rename_selected_fields(item: Any, mapping: dict[str, str]) -> Any:
    if not isinstance(item, dict):
        return item
    result = deepcopy(item)
    for old, new in mapping.items():
        value = get_path(result, old, default=MISSING)
        if value is MISSING:
            continue
        _delete_nested(result, split_path(old))
        _set_nested(result, split_path(new), value)
    return result


def numeric(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def position_tuple(position: Any) -> tuple[float, float, float] | None:
    if not isinstance(position, dict):
        return None
    try:
        return float(position["x"]), float(position["y"]), float(position["z"])
    except (KeyError, TypeError, ValueError):
        return None


def distance_between(a: Any, b: Any) -> float | None:
    pa = position_tuple(a)
    pb = position_tuple(b)
    if pa is None or pb is None:
        return None
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(pa, pb)))


def _set_nested(target: dict[str, Any], parts: list[str], value: Any) -> None:
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _delete_nested(target: dict[str, Any], parts: list[str]) -> None:
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        current = child
    current.pop(parts[-1], None)
