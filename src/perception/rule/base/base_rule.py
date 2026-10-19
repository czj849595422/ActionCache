"""
Base interfaces for perception rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from inspect import signature
from typing import Any, Callable, Protocol


@dataclass
class RuleContext:
    mode: str
    sub_mode: str
    radius: float | None
    agent_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseRule(Protocol):
    name: str

    def apply(self, data: Any, context: RuleContext) -> Any:
        ...


RuleLike = BaseRule | Callable[..., Any] | list[Any] | tuple[Any, ...] | dict[str, Any]


def call_rule_function(func: Callable[..., Any], data: Any, context: RuleContext) -> Any:
    """Call rule functions that accept either (data) or (data, context)."""
    params = signature(func).parameters
    positional = [
        p
        for p in params.values()
        if p.kind
        in (
            p.POSITIONAL_ONLY,
            p.POSITIONAL_OR_KEYWORD,
            p.VAR_POSITIONAL,
        )
    ]
    if any(p.kind == p.VAR_POSITIONAL for p in params.values()) or len(positional) >= 2:
        return func(data, context)
    return func(data)
