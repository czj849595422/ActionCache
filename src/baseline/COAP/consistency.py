"""双路动作一致性：动作名 + 配置的重要参数对比（同步版）。"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .ports import ActionCanonicalizerPort
from .schemas import Action, ActionSpec


_MISSING = object()
_POSITION_FIELDS = ("x", "y", "z")
_ITEM_LIST_SELECTORS = {"items[].name", "items[].destination"}


class ConsistencyError(ValueError):
    pass


class ImportantParameterActionConsistency:
    """比较动作名与人工配置的重要参数，判定双路动作是否等价。"""

    def __init__(
        self,
        important_parameters: Mapping[str, Sequence[str]],
        canonicalizer: ActionCanonicalizerPort,
        *,
        require_explicit_actions: bool = False,
    ) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for action_name, parameter_names in important_parameters.items():
            if not isinstance(action_name, str) or not action_name.strip():
                raise ConsistencyError("动作名必须是非空字符串")
            if isinstance(parameter_names, (str, bytes)) or not isinstance(
                parameter_names, (list, tuple)
            ):
                raise ConsistencyError(
                    f"{action_name} 的重要参数必须是序列"
                )
            names = tuple(str(item) for item in parameter_names)
            for name in names:
                _selector_root(name)
            if len(names) != len(set(names)):
                raise ConsistencyError(
                    f"{action_name} 的重要参数不能重复"
                )
            normalized[action_name] = names
        self._important_parameters = normalized
        self._canonicalizer = canonicalizer
        self._require_explicit_actions = require_explicit_actions

    def validate_action_space(self, action_space: Sequence[ActionSpec]) -> None:
        if isinstance(action_space, (str, bytes)) or not isinstance(
            action_space, (tuple, list)
        ):
            raise ConsistencyError("action_space 必须是 ActionSpec 序列")
        if not all(isinstance(item, ActionSpec) for item in action_space):
            raise ConsistencyError("action_space 必须包含 ActionSpec")
        specs = {item.name: item for item in action_space}
        if not specs and not self._require_explicit_actions:
            return
        unknown = set(self._important_parameters) - set(specs)
        if unknown:
            raise ConsistencyError(
                "重要参数引用了未知动作: " + ", ".join(sorted(unknown))
            )
        if self._require_explicit_actions:
            missing = set(specs) - set(self._important_parameters)
            if missing:
                raise ConsistencyError(
                    "以下动作未配置重要参数: " + ", ".join(sorted(missing))
                )
        for action_name, parameter_names in self._important_parameters.items():
            properties = specs[action_name].parameter_schema.get("properties", {})
            known = (
                set(properties) if isinstance(properties, Mapping) else set()
            )
            unknown_parameters = {
                name
                for name in parameter_names
                if not _selector_supported_by_schema(name, known)
            }
            if unknown_parameters:
                raise ConsistencyError(
                    f"{action_name} 的重要参数不在其 schema 中: "
                    + ", ".join(sorted(unknown_parameters))
                )

    def equivalent(self, left: Action, right: Action) -> bool:
        if not isinstance(left, Action) or not isinstance(right, Action):
            raise ConsistencyError("left 和 right 必须是 Action")
        normalized_left = self._canonicalizer.canonicalize(left)
        normalized_right = self._canonicalizer.canonicalize(right)
        if normalized_left.name != normalized_right.name:
            return False
        important = self._important_parameters.get(normalized_left.name, ())
        return all(
            _important_value(
                normalized_left.name,
                normalized_left.parameters,
                name,
            )
            == _important_value(
                normalized_right.name,
                normalized_right.parameters,
                name,
            )
            for name in important
        )


def _selector_root(selector: str) -> str:
    if not isinstance(selector, str) or not selector:
        raise ConsistencyError("重要参数名必须是非空字符串")
    if "[]." not in selector:
        return selector
    if selector not in _ITEM_LIST_SELECTORS:
        raise ConsistencyError(f"不支持的重要参数选择器: {selector}")
    return selector.split("[].", 1)[0]


def _selector_supported_by_schema(selector: str, known: set[str]) -> bool:
    root = _selector_root(selector)
    if root in known:
        return True
    return root == "position" and set(_POSITION_FIELDS) <= known


def _important_value(
    action_name: str,
    parameters: Mapping[str, Any],
    selector: str,
) -> Any:
    if selector == "position":
        return _position_value(parameters)
    if selector == "target" and action_name.startswith("combat."):
        return _combat_target_value(parameters.get("target", _MISSING))
    if selector == "item":
        return parameters.get("item", parameters.get("name", _MISSING))
    if selector in _ITEM_LIST_SELECTORS:
        return _item_list_values(
            parameters.get(_selector_root(selector), _MISSING),
            selector.rsplit(".", 1)[1],
        )
    return parameters.get(selector, _MISSING)


def _position_value(parameters: Mapping[str, Any]) -> Any:
    if "position" in parameters:
        position = parameters["position"]
        if isinstance(position, Mapping):
            return tuple(position.get(field, _MISSING) for field in _POSITION_FIELDS)
        return position
    if all(field in parameters for field in _POSITION_FIELDS):
        return tuple(parameters[field] for field in _POSITION_FIELDS)
    return _MISSING


def _combat_target_value(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return (("entity_id", value),)
    if isinstance(value, str):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)_(\d+)", value)
        if match:
            return (("entity_id", int(match.group(2))), ("name", match.group(1)))
        return (("name", value),)
    if isinstance(value, Mapping):
        return tuple(sorted(value.items(), key=lambda item: str(item[0])))
    return value


def _item_list_values(value: Any, field: str) -> Any:
    if value is _MISSING:
        return _MISSING
    items = value if isinstance(value, (list, tuple)) else (value,)
    return tuple(_item_value(item, field) for item in items)


def _item_value(item: Any, field: str) -> Any:
    if isinstance(item, str):
        return item if field == "name" else _MISSING
    if not isinstance(item, Mapping):
        return _MISSING
    if field == "name":
        return item.get("name", item.get("item", _MISSING))
    return item.get(field, _MISSING)


__all__ = ["ConsistencyError", "ImportantParameterActionConsistency"]
