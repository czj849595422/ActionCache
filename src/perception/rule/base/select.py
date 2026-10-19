"""
Path and field selection rules.
"""

from __future__ import annotations

from typing import Any

from .utils import (
    MISSING,
    apply_at_path,
    copy_selected_fields,
    drop_selected_fields,
    get_path,
    rename_selected_fields,
    set_path,
)


def select_path(path: str):
    """Return only the value at path, preserving the original path shape."""
    def rule(data: dict[str, Any], context):
        value = get_path(data, path, default=MISSING)
        return {} if value is MISSING else set_path(path, value)

    return rule


def pick_fields(path: str, fields: list[str]):
    """Keep only selected fields inside a dict or every dict in a list."""
    def transform(value, context):
        if isinstance(value, list):
            return [copy_selected_fields(item, fields) for item in value]
        return copy_selected_fields(value, fields)

    return apply_at_path(path, transform)


def drop_fields(path: str, fields: list[str]):
    """Drop selected fields inside a dict or every dict in a list."""
    def transform(value, context):
        if isinstance(value, list):
            return [drop_selected_fields(item, fields) for item in value]
        return drop_selected_fields(value, fields)

    return apply_at_path(path, transform)


def rename_fields(path: str, mapping: dict[str, str]):
    """Rename fields inside a dict or every dict in a list."""
    def transform(value, context):
        if isinstance(value, list):
            return [rename_selected_fields(item, mapping) for item in value]
        return rename_selected_fields(value, mapping)

    return apply_at_path(path, transform)
