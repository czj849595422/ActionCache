"""
Sorting and limiting rules.
"""

from __future__ import annotations

from typing import Any, Callable

from .utils import apply_at_path, as_list, field_value


def sort_by(path: str, key: str | Callable[..., Any], *, reverse: bool = False):
    """Sort list at path by a field path or key function."""
    def transform(value, context):
        return sorted(
            as_list(value),
            key=lambda item: _sort_key(field_value(item, key, context)),
            reverse=reverse,
        )

    return apply_at_path(path, transform)


def top_k(
    path: str,
    k: int,
    key: str | Callable[..., Any] | None = None,
    *,
    reverse: bool = False,
):
    """Keep the first k items, optionally after sorting."""
    def transform(value, context):
        items = as_list(value)
        if key is not None:
            items = sorted(
                items,
                key=lambda item: _sort_key(field_value(item, key, context)),
                reverse=reverse,
            )
        return items[:k]

    return apply_at_path(path, transform)


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, None)
    return (0, value)
