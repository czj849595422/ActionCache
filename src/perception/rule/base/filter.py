"""
Generic filtering rules.
"""

from __future__ import annotations

from typing import Any, Callable

from .utils import apply_at_path, as_list, field_value, numeric


Predicate = Callable[..., bool]


def filter_items(path: str, predicate: Predicate):
    """Filter a list at path with predicate(item) or predicate(item, context)."""
    def transform(value, context):
        result = []
        for item in as_list(value):
            try:
                keep = predicate(item, context)
            except TypeError:
                keep = predicate(item)
            if keep:
                result.append(item)
        return result

    return apply_at_path(path, transform)


def filter_by_value(path: str, field: str, value: Any):
    return filter_items(path, lambda item, context: field_value(item, field, context) == value)


def filter_by_contains(path: str, field: str, text: str, *, case_sensitive: bool = False):
    needle = text if case_sensitive else text.lower()

    def predicate(item, context):
        value = field_value(item, field, context)
        if value is None:
            return False
        haystack = str(value)
        if not case_sensitive:
            haystack = haystack.lower()
        return needle in haystack

    return filter_items(path, predicate)


def filter_by_range(
    path: str,
    field: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
):
    def predicate(item, context):
        value = numeric(field_value(item, field, context))
        if value is None:
            return False
        if min_value is not None and value < min_value:
            return False
        if max_value is not None and value > max_value:
            return False
        return True

    return filter_items(path, predicate)


def filter_not_none(path: str, field: str):
    return filter_items(path, lambda item, context: field_value(item, field, context) is not None)
