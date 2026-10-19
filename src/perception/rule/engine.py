"""
Rule execution helpers.
"""

from __future__ import annotations

from typing import Any

from .base.base_rule import RuleContext, RuleLike, call_rule_function


def normalize_rule(rule: RuleLike) -> RuleLike:
    """Normalize shorthand rule declarations into executable rule objects."""
    from .composition import ChainRule, ParallelRule

    if isinstance(rule, (list, tuple)):
        return ChainRule(list(rule))
    if isinstance(rule, dict):
        return ParallelRule(rule)
    return rule


def apply_rule(rule: RuleLike | None, data: Any, context: RuleContext) -> Any:
    """Apply a single rule, chain, parallel rule, or rule group."""
    if rule is None:
        return data

    normalized = normalize_rule(rule)
    if hasattr(normalized, "apply"):
        return normalized.apply(data, context)
    if callable(normalized):
        return call_rule_function(normalized, data, context)
    raise TypeError(f"Unsupported perception rule: {rule!r}")
