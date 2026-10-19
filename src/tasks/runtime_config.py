"""Task-level overrides for runner runtime options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any, TypeVar


ConfigT = TypeVar("ConfigT")

_RUNTIME_FIELDS = frozenset(
    {
        "system_messages_enabled",
        "system_message_interval",
    }
)


def apply_task_runtime_overrides(config: ConfigT, spec: Any) -> ConfigT:
    """Return ``config`` with supported values from the task specification.

    New task files should place runner-specific values under ``params.runtime``.
    The flat ``params`` form is retained for compatibility with early task files.
    Unrelated task parameters are ignored here and remain owned by their task
    manager.
    """

    params = getattr(spec, "params", None) or {}
    if not isinstance(params, Mapping):
        return config

    overrides = {
        name: params[name]
        for name in _RUNTIME_FIELDS
        if name in params
    }
    nested = params.get("runtime")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ValueError("params.runtime 必须是映射")
        overrides.update(
            {
                name: nested[name]
                for name in _RUNTIME_FIELDS
                if name in nested
            }
        )

    if not overrides:
        return config

    config_fields = {field.name for field in fields(config)}
    unsupported = sorted(set(overrides) - config_fields)
    if unsupported:
        raise ValueError(
            f"运行时配置不支持字段: {', '.join(unsupported)}"
        )
    return replace(config, **overrides)


__all__ = ["apply_task_runtime_overrides"]
