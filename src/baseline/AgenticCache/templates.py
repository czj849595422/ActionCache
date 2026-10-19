"""AgenticCache shorthand action templates and parameter rebinding."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Mapping, Sequence

from src.agents.action import (
    SimpleInstruction,
    SimpleInstructionRegistry,
    SkillRegistry,
)

from .schemas import ActionPlan


POSITION_BUCKET_SIZE = 8.0
_POSITION_FIELDS = ("x", "y", "z")
_COMBAT_ALIASES = {"attack", "fight", "fight_bow", "fight_melee", "retreat"}
_BOSS_ENTITY_NAMES = frozenset({"wither", "ender_dragon"})
_POSITION_ALIASES = {
    "goto",
    "follow",
    "break",
    "inspect_block",
    "mine",
    "place",
    "inspect_chest",
    "open",
}


class AgenticCacheTemplateError(ValueError):
    pass


def build_agentic_cache_instructions() -> SimpleInstructionRegistry:
    """Build the shared shorthand registry plus AgenticCache-local aliases."""
    registry = SimpleInstructionRegistry()
    _install_local_alias(
        registry,
        SimpleInstruction(
            alias="drop",
            action="inventory.drop",
            params=("items",),
            description="丢弃 items 中列出的物品。",
            description_en="Drop the listed items.",
        ),
    )
    _install_local_alias(
        registry,
        SimpleInstruction(
            alias="give",
            action="inventory.give",
            params=("target", "items"),
            description="把 items 中列出的物品交给 target。",
            description_en="Give the listed items to target.",
        ),
    )
    return registry


def validate_shorthand_coverage(
    registry: SkillRegistry,
    instructions: SimpleInstructionRegistry,
    *,
    enable_chat_actions: bool = False,
) -> None:
    missing: list[str] = []
    for skill_name, manifest in sorted(registry.manifests.items()):
        for command_name in sorted(manifest.commands):
            action = f"{skill_name}.{command_name}"
            if not registry.is_allowed(action):
                continue
            if action.startswith("chat.") and not enable_chat_actions:
                continue
            if instructions.instruction_for_action(action) is None:
                missing.append(action)
    if missing:
        raise AgenticCacheTemplateError(
            "AgenticCache requires shorthand aliases for all exposed actions: "
            + ", ".join(missing)
        )


def binding_context_from_observation(observation: Any) -> dict[str, Any]:
    """Extract a small structured context used to rebind dynamic parameters."""
    if not isinstance(observation, Mapping):
        return {}
    sections = observation.get("sections")
    if not isinstance(sections, Mapping):
        return {}
    return {
        "self": _first_fact(sections, "self_state"),
        "hostile_entities": _facts(sections, "hostile_entities"),
        "players": _facts(sections, "players"),
        "dropped_items": _facts(sections, "dropped_items"),
        "nearby_blocks": _facts(sections, "nearby_blocks"),
        "containers": _facts(sections, "containers"),
        "resources": _facts(sections, "resources"),
        "processors": _facts(sections, "processors"),
    }


def template_plan_from_call(
    *,
    alias: str,
    input_params: Mapping[str, Any],
    input_positional: Sequence[Any],
    canonical_name: str,
    canonical_parameters: Mapping[str, Any],
    binding_context: Mapping[str, Any] | None = None,
    action_text: str = "",
) -> ActionPlan:
    if "." in alias:
        raise AgenticCacheTemplateError(
            f"AgenticCache cache actions must use shorthand aliases: {alias!r}"
        )
    template_parameters = _template_parameters(
        alias,
        input_params,
        input_positional,
        canonical_parameters,
        binding_context or {},
    )
    return ActionPlan(
        name=alias,
        parameters=template_parameters,
        input_positional=list(input_positional),
        canonical_name=canonical_name,
        canonical_parameters=dict(canonical_parameters),
        action_text=action_text or render_action_call(alias, input_params, input_positional),
    )


def template_plan_from_unit(
    unit: Any,
    *,
    instructions: SimpleInstructionRegistry | None,
    binding_context: Mapping[str, Any] | None = None,
    input_params: Mapping[str, Any] | None = None,
    input_positional: Sequence[Any] | None = None,
) -> ActionPlan:
    canonical_name = str(getattr(unit, "action", "") or "")
    canonical_parameters = dict(getattr(unit, "params", None) or {})
    alias = alias_for_canonical(canonical_name, instructions)
    params = dict(input_params or canonical_parameters)
    positional = list(input_positional or [])
    return template_plan_from_call(
        alias=alias,
        input_params=params,
        input_positional=positional,
        canonical_name=canonical_name,
        canonical_parameters=canonical_parameters,
        binding_context=binding_context,
    )


def migrate_v1_plan_to_template(
    plan: ActionPlan,
    *,
    instructions: SimpleInstructionRegistry | None,
) -> ActionPlan | None:
    if plan.name == "START":
        return plan
    if "." not in plan.name:
        return plan
    try:
        alias = alias_for_canonical(plan.name, instructions)
    except AgenticCacheTemplateError:
        return None
    return template_plan_from_call(
        alias=alias,
        input_params=plan.parameters,
        input_positional=plan.input_positional,
        canonical_name=plan.name,
        canonical_parameters=plan.parameters,
        action_text=plan.action_text,
    )


def instantiate_template_plan(
    plan: ActionPlan,
    *,
    binding_context: Mapping[str, Any] | None,
    instructions: SimpleInstructionRegistry | None,
) -> tuple[str, dict[str, Any], str, dict[str, Any], list[Any], ActionPlan]:
    if plan.name == "START":
        raise AgenticCacheTemplateError("START is not executable")
    if "." in plan.name:
        raise AgenticCacheTemplateError(
            f"cached action must use shorthand alias, got {plan.name!r}"
        )
    if instructions is None:
        raise AgenticCacheTemplateError("shorthand instruction registry is required")
    params, positional = _instantiate_inputs(plan, binding_context or {})
    try:
        canonical, filled, mode = instructions.expand(plan.name, params, positional)
    except Exception as exc:  # noqa: BLE001
        raise AgenticCacheTemplateError(str(exc)) from exc
    executed = ActionPlan(
        name=plan.name,
        parameters=dict(plan.parameters),
        input_positional=list(positional),
        canonical_name=canonical,
        canonical_parameters=dict(filled),
        action_text=render_action_call(plan.name, params, positional),
    )
    return canonical, filled, mode, params, positional, executed


def alias_for_canonical(
    canonical_name: str,
    instructions: SimpleInstructionRegistry | None,
) -> str:
    if canonical_name == "START":
        return "START"
    if instructions is None:
        if "." in canonical_name:
            raise AgenticCacheTemplateError(
                f"missing shorthand registry for canonical action {canonical_name!r}"
            )
        return canonical_name
    instruction = instructions.instruction_for_action(canonical_name)
    if instruction is None:
        raise AgenticCacheTemplateError(
            f"no AgenticCache shorthand alias for {canonical_name!r}"
        )
    return instruction.alias


def render_action_call(
    alias: str,
    params: Mapping[str, Any],
    positional: Sequence[Any],
) -> str:
    args = [repr(value) for value in positional]
    args.extend(f"{key}={value!r}" for key, value in params.items())
    return f"{alias}({', '.join(args)})"


def _install_local_alias(
    registry: SimpleInstructionRegistry,
    instruction: SimpleInstruction,
) -> None:
    if instruction.alias in registry.catalog.instructions:
        return
    try:
        registry.skills.resolve(instruction.action, enforce_allowlist=False)
    except Exception:  # noqa: BLE001
        return
    registry.catalog.instructions[instruction.alias] = instruction
    registry.catalog.by_action.setdefault(instruction.action, instruction)


def _facts(sections: Mapping[str, Any], section_name: str) -> list[dict[str, Any]]:
    section = sections.get(section_name)
    if not isinstance(section, Mapping):
        return []
    facts = section.get("facts")
    if not isinstance(facts, list):
        return []
    return [dict(item) for item in facts[:32] if isinstance(item, Mapping)]


def _first_fact(sections: Mapping[str, Any], section_name: str) -> dict[str, Any]:
    facts = _facts(sections, section_name)
    return facts[0] if facts else {}


def _template_parameters(
    alias: str,
    input_params: Mapping[str, Any],
    input_positional: Sequence[Any],
    canonical_parameters: Mapping[str, Any],
    binding_context: Mapping[str, Any],
) -> dict[str, Any]:
    if alias in _COMBAT_ALIASES:
        return _combat_template(alias, input_params, input_positional, canonical_parameters)
    if alias == "pick":
        return _pick_template(input_params, canonical_parameters, binding_context)
    if alias in _POSITION_ALIASES:
        return _position_template(input_params, canonical_parameters, binding_context)
    if input_params:
        return _json_copy(dict(input_params))
    if input_positional:
        return {}
    return _json_copy(dict(canonical_parameters))


def _combat_template(
    alias: str,
    input_params: Mapping[str, Any],
    input_positional: Sequence[Any],
    canonical_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    target = canonical_parameters.get("target")
    kind = _target_kind(target)
    template: dict[str, Any] = {"target_selector": "nearest_hostile"}
    if kind:
        template["kind"] = kind
    for key in ("weapon", "bow", "distance", "collect_drops", "food", "combat_type"):
        if key in input_params or _positional_supplies(alias, key, input_positional):
            if key in canonical_parameters:
                template[key] = _json_copy(canonical_parameters[key])
    return template


def _positional_supplies(alias: str, key: str, positional: Sequence[Any]) -> bool:
    if not positional:
        return False
    if alias == "fight_melee" and key == "weapon":
        return True
    if alias == "fight_bow" and key == "bow":
        return True
    if alias == "fight" and key == "weapon" and len(positional) >= 2:
        return True
    return False


def _pick_template(
    input_params: Mapping[str, Any],
    canonical_parameters: Mapping[str, Any],
    binding_context: Mapping[str, Any],
) -> dict[str, Any]:
    positions = canonical_parameters.get("positions")
    template: dict[str, Any] = {}
    item_name = _item_name_for_positions(positions, binding_context)
    if item_name:
        template["item_selector"] = "nearest_dropped_item"
        template["item_name"] = item_name
    else:
        template["positions"] = _json_copy(positions)
    if "mode" in input_params or "mode" in canonical_parameters:
        template["mode"] = _json_copy(canonical_parameters.get("mode"))
    return template


def _position_template(
    input_params: Mapping[str, Any],
    canonical_parameters: Mapping[str, Any],
    binding_context: Mapping[str, Any],
) -> dict[str, Any]:
    if "entity_name" in canonical_parameters:
        return {"entity_name": canonical_parameters["entity_name"]}
    position = _canonical_position(canonical_parameters)
    if position is None:
        if input_params:
            return _json_copy(dict(input_params))
        return _json_copy(dict(canonical_parameters))
    selector = _selector_for_position(position, binding_context)
    if selector:
        return selector
    bucket = position_bucket(position)
    return {
        "position_bucket": bucket,
        "position": position_from_bucket(bucket),
    }


def _instantiate_inputs(
    plan: ActionPlan,
    binding_context: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Any]]:
    params = dict(plan.parameters)
    if plan.name in _COMBAT_ALIASES and params.get("target_selector") == "nearest_hostile":
        target = _select_hostile(binding_context, params.get("kind"))
        if target is None:
            raise AgenticCacheTemplateError("no matching hostile entity for cached combat action")
        result = {
            key: value
            for key, value in params.items()
            if key not in {"target_selector", "kind"}
        }
        result["target"] = _target_payload(target, params.get("kind"))
        return result, []
    if plan.name == "pick" and params.get("item_selector") == "nearest_dropped_item":
        item = _select_dropped_item(binding_context, params.get("item_name"))
        if item is None:
            raise AgenticCacheTemplateError("no matching dropped item for cached pick action")
        position = _position(item.get("position"))
        if position is None:
            raise AgenticCacheTemplateError("matching dropped item has no position")
        result = {key: value for key, value in params.items() if key not in {"item_selector", "item_name"}}
        result["positions"] = [position]
        return result, []
    if "position_selector" in params:
        position = _select_position(binding_context, params)
        if position is None:
            raise AgenticCacheTemplateError("no matching current position for cached action")
        result = {
            key: value
            for key, value in params.items()
            if key not in {"position_selector", "kind", "block_name", "item_name"}
        }
        result["position"] = position
        return result, []
    return params, list(plan.input_positional)


def _target_payload(fact: Mapping[str, Any], fallback_kind: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    name = fact.get("kind") or fact.get("name") or fallback_kind
    name_key = str(name or "").split(":", 1)[-1].lower()
    entity_id = fact.get("entity_id")
    if entity_id is None:
        entity_id = fact.get("id")
    if entity_id is None:
        entity_id = fact.get("entityId")
    if "boss" in (fact.get("roles") or []) or name_key in _BOSS_ENTITY_NAMES:
        payload = {"name": name} if name else {"name": str(fallback_kind or "unknown")}
        if isinstance(entity_id, int) and not isinstance(entity_id, bool):
            payload["entity_id"] = entity_id
        elif isinstance(entity_id, str) and entity_id.strip().isdigit():
            payload["entity_id"] = int(entity_id.strip())
        return payload
    if name:
        payload["name"] = name
    if entity_id is not None:
        payload["entity_id"] = entity_id
    uuid = fact.get("uuid")
    if uuid is not None:
        payload["uuid"] = uuid
    return payload or {"name": str(fallback_kind or "unknown")}


def _select_hostile(
    binding_context: Mapping[str, Any],
    kind: Any,
) -> dict[str, Any] | None:
    hostiles = [
        item
        for item in binding_context.get("hostile_entities", [])
        if isinstance(item, Mapping)
    ]
    if kind:
        hostiles = [
            item
            for item in hostiles
            if str(item.get("kind") or item.get("name") or "") == str(kind)
        ]
    return _nearest(hostiles)


def _select_dropped_item(
    binding_context: Mapping[str, Any],
    item_name: Any,
) -> dict[str, Any] | None:
    items = [
        item
        for item in binding_context.get("dropped_items", [])
        if isinstance(item, Mapping)
    ]
    if item_name:
        items = [
            item
            for item in items
            if _dropped_item_name(item) == str(item_name)
        ]
    return _nearest(items)


def _select_position(
    binding_context: Mapping[str, Any],
    selector: Mapping[str, Any],
) -> dict[str, float] | None:
    kind = selector.get("kind")
    if selector.get("position_selector") == "hostile":
        selected = _select_hostile(binding_context, kind)
        return None if selected is None else _position(selected.get("position"))
    if selector.get("position_selector") == "dropped_item":
        selected = _select_dropped_item(binding_context, selector.get("item_name"))
        return None if selected is None else _position(selected.get("position"))
    block_name = selector.get("block_name")
    blocks = _all_position_facts(binding_context)
    if block_name:
        blocks = [
            item
            for item in blocks
            if str(item.get("block_name") or item.get("name") or "") == str(block_name)
        ]
    selected = _nearest(blocks)
    return None if selected is None else _position(selected.get("position"))


def _selector_for_position(
    position: Mapping[str, Any],
    binding_context: Mapping[str, Any],
) -> dict[str, Any] | None:
    for fact in binding_context.get("hostile_entities", []):
        if _near(position, fact.get("position")):
            kind = fact.get("kind") or fact.get("name")
            result = {"position_selector": "hostile"}
            if kind:
                result["kind"] = kind
            return result
    for fact in binding_context.get("dropped_items", []):
        if _near(position, fact.get("position")):
            name = _dropped_item_name(fact)
            result = {"position_selector": "dropped_item"}
            if name:
                result["item_name"] = name
            return result
    for fact in _all_position_facts(binding_context):
        if _near(position, fact.get("position")):
            block_name = fact.get("block_name") or fact.get("name")
            result = {"position_selector": "block"}
            if block_name:
                result["block_name"] = block_name
            return result
    return None


def _all_position_facts(binding_context: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for section in ("nearby_blocks", "containers", "resources", "processors"):
        for item in binding_context.get(section, []):
            if isinstance(item, Mapping):
                values.append(dict(item))
    return values


def _item_name_for_positions(
    positions: Any,
    binding_context: Mapping[str, Any],
) -> str | None:
    if not isinstance(positions, list) or not positions:
        return None
    for position in positions:
        for item in binding_context.get("dropped_items", []):
            if isinstance(item, Mapping) and _near(position, item.get("position")):
                return _dropped_item_name(item)
    return None


def _dropped_item_name(item: Mapping[str, Any]) -> str | None:
    value = item.get("item")
    if isinstance(value, Mapping):
        name = value.get("name") or value.get("displayName")
        return None if name is None else str(name)
    name = item.get("name")
    return None if name is None else str(name)


def _target_kind(target: Any) -> str | None:
    if isinstance(target, Mapping):
        value = target.get("name") or target.get("kind") or target.get("type")
        return None if value is None else str(value)
    if isinstance(target, str):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)_(\d+)", target)
        return match.group(1) if match else target
    return None


def _canonical_position(parameters: Mapping[str, Any]) -> dict[str, float] | None:
    if isinstance(parameters.get("position"), Mapping):
        return _position(parameters.get("position"))
    if all(axis in parameters for axis in _POSITION_FIELDS):
        return _position(parameters)
    return None


def position_bucket(position: Mapping[str, Any], *, size: float = POSITION_BUCKET_SIZE) -> str:
    pos = _position(position)
    if pos is None:
        raise AgenticCacheTemplateError(f"invalid position for bucket: {position!r}")
    parts = [str(math.floor(float(pos[axis]) / size)) for axis in _POSITION_FIELDS]
    return ":".join(parts)


def position_from_bucket(bucket: str, *, size: float = POSITION_BUCKET_SIZE) -> dict[str, float]:
    parts = bucket.split(":")
    if len(parts) != 3:
        raise AgenticCacheTemplateError(f"invalid position bucket: {bucket!r}")
    return {
        axis: (float(int(parts[index])) * size) + (size / 2.0)
        for index, axis in enumerate(_POSITION_FIELDS)
    }


def _near(left: Any, right: Any, *, max_distance: float = 1.5) -> bool:
    a = _position(left)
    b = _position(right)
    if a is None or b is None:
        return False
    return sum((a[axis] - b[axis]) ** 2 for axis in _POSITION_FIELDS) ** 0.5 <= max_distance


def _nearest(items: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return dict(
        min(
            items,
            key=lambda item: (
                float(item.get("distance"))
                if isinstance(item.get("distance"), (int, float))
                and not isinstance(item.get("distance"), bool)
                else float("inf")
            ),
        )
    )


def _position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return {axis: float(value[axis]) for axis in _POSITION_FIELDS}
    except (KeyError, TypeError, ValueError):
        return None


def _json_copy(value: Any) -> Any:
    return deepcopy(value)


__all__ = [
    "AgenticCacheTemplateError",
    "POSITION_BUCKET_SIZE",
    "alias_for_canonical",
    "binding_context_from_observation",
    "build_agentic_cache_instructions",
    "instantiate_template_plan",
    "migrate_v1_plan_to_template",
    "position_bucket",
    "position_from_bucket",
    "render_action_call",
    "template_plan_from_call",
    "template_plan_from_unit",
    "validate_shorthand_coverage",
]
