"""Task-independent Minecraft world fact rules (W01-W16)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from .base import RuleContext
from .composition import RuleGroup
from src.perception.filters import filter_observer_players
from src.perception.providers import BlockEntityProvider, BlockEntityQueryError


WORLD_SCHEMA_VERSION = "world_perception.v1"
WORLD_SECTION_NAMES = (
    "self_state",
    "held_and_equipment",
    "inventory",
    "players",
    "hostile_entities",
    "passive_neutral_entities",
    "projectiles_explosives",
    "other_entities",
    "dropped_items",
    "nearby_blocks",
    "resources",
    "containers",
    "processors",
    "workstations",
    "world_environment",
    "observation_coverage",
)


_PROJECTILE_NAMES = {
    "arrow",
    "spectral_arrow",
    "trident",
    "fireball",
    "small_fireball",
    "dragon_fireball",
    "wither_skull",
    "llama_spit",
    "shulker_bullet",
    "fishing_bobber",
    "tnt",
    "tnt_minecart",
    "firework_rocket",
    "potion",
    "experience_bottle",
    "egg",
    "snowball",
    "ender_pearl",
}
_BOSS_NAMES = {"wither", "ender_dragon"}
_NEUTRAL_NAMES = {
    "bee",
    "dolphin",
    "enderman",
    "goat",
    "iron_golem",
    "llama",
    "panda",
    "piglin",
    "polar_bear",
    "wolf",
    "zombified_piglin",
}
_UTILITY_NAMES = {"allay", "iron_golem", "snow_golem", "villager", "wandering_trader"}
_RESOURCE_EXACT = {
    "wheat",
    "carrots",
    "potatoes",
    "beetroots",
    "nether_wart",
    "cocoa",
    "sweet_berry_bush",
    "sugar_cane",
    "cactus",
    "bamboo",
    "melon",
    "pumpkin",
    "clay",
    "gravel",
    "sand",
    "red_sand",
    "ancient_debris",
    "amethyst_cluster",
}
_RESOURCE_SUFFIXES = (
    "_ore",
    "_log",
    "_wood",
    "_stem",
    "_hyphae",
    "_mineral_block",
)
_MINERAL_BLOCKS = {
    "coal_block",
    "copper_block",
    "diamond_block",
    "emerald_block",
    "gold_block",
    "iron_block",
    "lapis_block",
    "netherite_block",
    "raw_copper_block",
    "raw_gold_block",
    "raw_iron_block",
    "redstone_block",
}
_GROWTH_MAX = {
    "wheat": 7,
    "carrots": 7,
    "potatoes": 7,
    "beetroots": 3,
    "nether_wart": 3,
    "cocoa": 2,
    "sweet_berry_bush": 3,
}
_PROCESSOR_NAMES = {"furnace", "smoker", "blast_furnace"}
_WORKSTATION_NAMES = {
    "crafting_table",
    "anvil",
    "chipped_anvil",
    "damaged_anvil",
    "enchanting_table",
    "stonecutter",
    "loom",
    "grindstone",
    "smithing_table",
    "cartography_table",
    "fletching_table",
}


@dataclass(frozen=True)
class WorldRuleOptions:
    container_internal_enabled: bool = True
    processor_internal_enabled: bool = True
    use_range: float = 4.5
    require_line_of_sight: bool = True

    def __post_init__(self) -> None:
        if self.use_range <= 0:
            raise ValueError("use_range must be positive")


@dataclass
class WorldSnapshotRule:
    options: WorldRuleOptions
    provider: BlockEntityProvider | None = None
    name: str = "world_snapshot"

    def apply(self, data: Any, context: RuleContext) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise TypeError("world_snapshot expects a perception dict")
        active_provider = self.provider or context.metadata.get("block_entity_provider")
        filtered = filter_observer_players(data)
        return _build_world_snapshot(filtered, context, self.options, active_provider)


def world_snapshot(
    *,
    options: WorldRuleOptions | None = None,
    provider: BlockEntityProvider | None = None,
) -> RuleGroup:
    active_options = options or WorldRuleOptions()
    return RuleGroup(
        "world_snapshot",
        WorldSnapshotRule(active_options, provider),
        {
            "schema_version": WORLD_SCHEMA_VERSION,
            "use_range": active_options.use_range,
            "require_line_of_sight": active_options.require_line_of_sight,
        },
    )


def select_world_sections(*section_names: str) -> RuleGroup:
    requested = tuple(dict.fromkeys(section_names))
    unknown = sorted(set(requested) - set(WORLD_SECTION_NAMES))
    if unknown:
        raise ValueError(f"unknown world perception sections: {unknown}")

    def select(data: Any, context: RuleContext) -> dict[str, Any]:
        if not isinstance(data, dict) or not isinstance(data.get("sections"), dict):
            raise TypeError("select_world_sections expects a world perception snapshot")
        return {
            "schema_version": data.get("schema_version", WORLD_SCHEMA_VERSION),
            "sections": {
                name: data["sections"][name]
                for name in requested
                if name in data["sections"]
            },
        }

    return RuleGroup("select_world_sections", select, {"sections": list(requested)})


def _build_world_snapshot(
    raw: dict[str, Any],
    context: RuleContext,
    options: WorldRuleOptions,
    provider: BlockEntityProvider | None,
) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    timestamp = _int_or_none(metadata.get("timestamp"))
    coverage = {
        "mode": metadata.get("mode", context.mode),
        "sub_mode": metadata.get("subMode", context.sub_mode),
        "radius": metadata.get("radius", context.radius),
        "block_radius": metadata.get("blockRadius"),
        "fov_degrees": metadata.get("fovDegrees"),
    }
    self_raw = raw.get("self") if isinstance(raw.get("self"), dict) else {}
    self_position = _position(self_raw.get("position"))
    self_yaw = _number(self_raw.get("yaw"))

    self_fact = _self_fact(self_raw, context.agent_name)
    equipment_fact = _equipment_fact(self_raw)
    inventory_fact = _inventory_fact(self_raw)

    blocks = [_normalize_block(value) for value in _dict_list(raw.get("blocks"))]
    blocks.sort(key=_distance_sort_key)
    visible_entity_ids = {
        value.get("id")
        for value in _dict_list(raw.get("entities"))
        if value.get("id") is not None
    }
    players = _player_facts(
        raw,
        context,
        self_position,
        self_yaw,
        blocks,
        visible_entity_ids,
    )

    hostiles: list[dict[str, Any]] = []
    passive_neutral: list[dict[str, Any]] = []
    projectiles: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for entity in _dict_list(raw.get("entities")):
        if entity.get("username") or str(entity.get("type") or "").lower() == "player":
            continue
        if _is_dropped_item_entity(entity):
            continue
        fact = _entity_fact(entity, self_position, self_yaw, blocks)
        name = fact["kind"]
        if _is_projectile(entity, name):
            fact["moving_toward_self"] = _moving_toward_self(entity, self_position)
            fact["owner_id"] = entity.get("ownerId") or entity.get("owner_id")
            fact["fuse_ticks"] = entity.get("fuseTicks") or entity.get("fuse_ticks")
            projectiles.append(fact)
        elif _is_hostile(entity, name):
            fact["entity_class"] = "hostile"
            fact["roles"] = ["boss"] if name in _BOSS_NAMES else []
            hostiles.append(fact)
        elif _is_living_entity(entity):
            fact["entity_class"] = _passive_class(entity, name)
            passive_neutral.append(fact)
        else:
            others.append(fact)

    for values in (hostiles, passive_neutral, projectiles, others):
        values.sort(key=_distance_sort_key)

    dropped_items = [_dropped_item_fact(value) for value in _dict_list(raw.get("items"))]
    dropped_items.sort(key=_distance_sort_key)
    resources = _resource_facts(blocks)
    container_facts, container_errors = _container_facts(
        blocks, self_position, options, provider
    )
    processor_facts, processor_errors = _processor_facts(
        blocks, self_position, options, provider
    )
    workstations = [
        _workstation_fact(block, self_position, self_yaw, options, blocks)
        for block in blocks
        if block.get("block_name") in _WORKSTATION_NAMES
    ]
    environment_fact = _environment_fact(raw, blocks)
    coverage_fact = _coverage_fact(raw, [*container_errors, *processor_errors])

    sections = {
        "self_state": _section(
            [self_fact] if self_fact else [], ["mineflayer_raw", "derived"], timestamp, coverage,
            unknown_reason=None if self_fact else "self state missing",
        ),
        "held_and_equipment": _section(
            [equipment_fact] if equipment_fact else [], ["mineflayer_raw", "derived"], timestamp, coverage,
            unknown_reason=None if equipment_fact else "equipment state missing",
        ),
        "inventory": _section(
            [inventory_fact] if inventory_fact else [], ["mineflayer_raw", "derived"], timestamp, coverage,
            unknown_reason=None if inventory_fact else "inventory state missing",
        ),
        "players": _section(players, ["mineflayer_raw", "derived"], timestamp, coverage),
        "hostile_entities": _section(hostiles, ["mineflayer_raw", "derived"], timestamp, coverage),
        "passive_neutral_entities": _section(passive_neutral, ["mineflayer_raw", "derived"], timestamp, coverage),
        "projectiles_explosives": _section(projectiles, ["mineflayer_raw", "derived"], timestamp, coverage),
        "other_entities": _section(others, ["mineflayer_raw", "derived"], timestamp, coverage),
        "dropped_items": _section(
            dropped_items,
            ["mineflayer_raw", "derived"],
            timestamp,
            coverage,
            summary={"totals": _aggregate_items([fact["item"] for fact in dropped_items])},
        ),
        "nearby_blocks": _section(blocks, ["mineflayer_raw"], timestamp, coverage),
        "resources": _section(resources, ["mineflayer_raw", "derived"], timestamp, coverage),
        "containers": _section(
            container_facts,
            _enhancement_sources(container_facts),
            timestamp,
            coverage,
            summary={"query_errors": container_errors},
        ),
        "processors": _section(
            processor_facts,
            _enhancement_sources(processor_facts),
            timestamp,
            coverage,
            summary={"query_errors": processor_errors},
        ),
        "workstations": _section(
            workstations, ["mineflayer_raw", "derived"], timestamp, coverage
        ),
        "world_environment": _section(
            [environment_fact] if environment_fact else [],
            ["mineflayer_raw", "derived"],
            timestamp,
            coverage,
            unknown_reason=None if environment_fact else "environment state missing",
        ),
        "observation_coverage": _section(
            [coverage_fact], ["mineflayer_raw"], timestamp, coverage
        ),
    }
    return {"schema_version": WORLD_SCHEMA_VERSION, "sections": sections}


def _section(
    facts: list[dict[str, Any]],
    sources: list[str],
    observed_at_ms: int | None,
    coverage: dict[str, Any],
    *,
    summary: dict[str, Any] | None = None,
    unknown_reason: str | None = None,
) -> dict[str, Any]:
    if unknown_reason:
        status = "unknown"
    else:
        status = "observed" if facts else "empty"
    value = {
        "status": status,
        "sources": list(dict.fromkeys(sources)),
        "freshness": "live",
        "observed_at_ms": observed_at_ms,
        "coverage": dict(coverage),
        "facts": facts,
        "summary": summary or {},
    }
    if unknown_reason:
        value["reason"] = unknown_reason
    return value


def _self_fact(
    value: dict[str, Any], fallback_agent_name: str | None = None
) -> dict[str, Any] | None:
    if not value:
        return None
    position = _position(value.get("position"))
    velocity = _vector(value.get("velocity"))
    health = _number(value.get("health"))
    max_health = _number(value.get("maxHealth"))
    return {
        "agent_name": value.get("username") or fallback_agent_name,
        "position": position,
        "yaw": _number(value.get("yaw")),
        "pitch": _number(value.get("pitch")),
        "velocity": velocity,
        "movement_state": _movement_state(velocity),
        "health": health,
        "max_health": max_health if max_health is not None else (20 if health is not None else None),
        "attack_event_sequence": _int_or_none(value.get("attackEventSequence")),
        "last_attack_at": _number(value.get("lastAttackAt")),
        "absorption": _number(value.get("absorption")),
        "food": _number(value.get("food")),
        "max_food": _number(value.get("maxFood")) or 20,
        "food_saturation": _number(value.get("foodSaturation")),
        "oxygen": _number(value.get("oxygenLevel")),
        "max_oxygen": _number(value.get("maxOxygenLevel")) or 20,
        "game_mode": value.get("gameMode"),
        "dimension": value.get("dimension"),
        "on_ground": value.get("onGround"),
        "in_water": value.get("isInWater"),
        "in_lava": value.get("isInLava"),
        "sleeping": value.get("isSleeping"),
        "burning": value.get("isBurning"),
        "effects": value.get("effects") or {},
    }


def _equipment_fact(value: dict[str, Any]) -> dict[str, Any] | None:
    if not value:
        return None
    slots = value.get("equipmentSlots") if isinstance(value.get("equipmentSlots"), dict) else {}
    main_hand = _normalize_item(slots.get("mainHand") or value.get("heldItem"))
    result = {
        "main_hand": main_hand,
        "off_hand": _normalize_item(slots.get("offHand")),
        "quick_bar_slot": _int_or_none(value.get("quickBarSlot")),
        "head": _normalize_item(slots.get("head")),
        "chest": _normalize_item(slots.get("chest")),
        "legs": _normalize_item(slots.get("legs")),
        "feet": _normalize_item(slots.get("feet")),
    }
    if not slots and isinstance(value.get("equipment"), list):
        result["unassigned"] = [
            item for item in (_normalize_item(entry) for entry in value["equipment"]) if item
        ]
    return result


def _inventory_fact(value: dict[str, Any]) -> dict[str, Any] | None:
    if not value:
        return None
    stacks = [
        item for item in (_normalize_item(entry) for entry in _dict_list(value.get("inventory"))) if item
    ]
    slots = {item["slot"] for item in stacks if item.get("slot") is not None}
    total_slots = 36
    return {
        "stacks": stacks,
        "totals": _aggregate_items(stacks),
        "used_slots": len(slots) if slots else len(stacks),
        "empty_slots": max(0, total_slots - (len(slots) if slots else len(stacks))),
        "total_slots": total_slots,
    }


def _player_facts(
    raw: dict[str, Any],
    context: RuleContext,
    self_position: dict[str, float] | None,
    self_yaw: float | None,
    blocks: list[dict[str, Any]],
    visible_entity_ids: set[Any],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for player in _dict_list(raw.get("players")):
        if player.get("isSelf"):
            continue
        if _is_spectator_game_mode(player.get("gamemode")):
            continue
        entity = player.get("entity") if isinstance(player.get("entity"), dict) else {}
        if not entity or entity.get("id") not in visible_entity_ids:
            continue
        distance = _number(entity.get("distance"))
        if context.radius is not None and (distance is None or distance > context.radius):
            continue
        base = _entity_fact(entity, self_position, self_yaw, blocks)
        base["player_name"] = player.get("username")
        base["game_mode"] = player.get("gamemode")
        base["ping"] = player.get("ping")
        facts.append(base)
    facts.sort(key=_distance_sort_key)
    return facts


def _entity_fact(
    entity: dict[str, Any],
    self_position: dict[str, float] | None,
    self_yaw: float | None,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    position = _position(entity.get("position"))
    velocity = _vector(entity.get("velocity"))
    slots = entity.get("equipmentSlots") if isinstance(entity.get("equipmentSlots"), dict) else {}
    return {
        "entity_id": entity.get("id") or entity.get("entityId"),
        "uuid": entity.get("uuid"),
        "kind": _entity_name(entity),
        "position": position,
        "distance": _number(entity.get("distance")),
        "relative_direction": _relative_direction(self_position, self_yaw, position),
        "yaw": _number(entity.get("yaw")),
        "pitch": _number(entity.get("pitch")),
        "velocity": velocity,
        "movement_state": _movement_state(velocity),
        "line_of_sight": _has_line_of_sight(self_position, position, blocks),
        "health": _number(entity.get("health")),
        "max_health": _number(entity.get("maxHealth")),
        "held_item": _normalize_item(entity.get("heldItem")),
        "equipment": {
            "off_hand": _normalize_item(slots.get("offHand")),
            "head": _normalize_item(slots.get("head")),
            "chest": _normalize_item(slots.get("chest")),
            "legs": _normalize_item(slots.get("legs")),
            "feet": _normalize_item(slots.get("feet")),
        },
        "effects": entity.get("effects") or {},
        "is_baby": entity.get("isBaby"),
        "is_burning": entity.get("isBurning"),
    }


def _dropped_item_fact(value: dict[str, Any]) -> dict[str, Any]:
    item = _normalize_item(value.get("item")) or {"name": "unknown", "count": None}
    return {
        "entity_id": value.get("entityId") or value.get("id"),
        "uuid": value.get("uuid"),
        "item": item,
        "position": _position(value.get("position")),
        "distance": _number(value.get("distance")),
        "velocity": _vector(value.get("velocity")),
    }


def _normalize_block(value: dict[str, Any]) -> dict[str, Any]:
    position = _block_position(value.get("position"))
    harvest_names = value.get("harvestToolNames")
    if not isinstance(harvest_names, list):
        harvest = value.get("harvestTools") if isinstance(value.get("harvestTools"), dict) else {}
        harvest_names = sorted(str(key) for key, enabled in harvest.items() if enabled)
    return {
        "block_name": _strip_namespace(value.get("name") or "unknown"),
        "position": position,
        "distance": _number(value.get("distance")),
        "material": value.get("material"),
        "bounding_box": value.get("boundingBox"),
        "transparent": value.get("transparent"),
        "hardness": _number(value.get("hardness")),
        "diggable": value.get("diggable"),
        "harvest_tools": harvest_names,
        "properties": value.get("properties") if isinstance(value.get("properties"), dict) else {},
        "light": _number(value.get("light")),
        "sky_light": _number(value.get("skyLight")),
        "biome": value.get("biome"),
    }


def _resource_facts(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[tuple[int, int, int], dict[str, Any]]] = {}
    for block in blocks:
        name = block["block_name"]
        position = _position_tuple(block.get("position"))
        if position is None or not _is_resource(name):
            continue
        by_name.setdefault(name, {})[position] = block

    facts: list[dict[str, Any]] = []
    for name in sorted(by_name):
        remaining = set(by_name[name])
        group_index = 0
        while remaining:
            anchor = min(remaining)
            queue = [anchor]
            remaining.remove(anchor)
            positions: list[tuple[int, int, int]] = []
            while queue:
                current = queue.pop()
                positions.append(current)
                for neighbor in _face_neighbors(current):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        queue.append(neighbor)
            group_blocks = [by_name[name][position] for position in positions]
            nearest = min(group_blocks, key=_distance_sort_key)
            properties = nearest.get("properties") or {}
            growth = _int_or_none(properties.get("age"))
            max_growth = _GROWTH_MAX.get(name)
            facts.append(
                {
                    "resource_name": name,
                    "block_count": len(positions),
                    "positions": [_tuple_position(position) for position in sorted(positions)],
                    "nearest_position": nearest.get("position"),
                    "nearest_distance": nearest.get("distance"),
                    "group_id": f"{name}@{anchor[0]},{anchor[1]},{anchor[2]}#{group_index}",
                    "diggable": all(block.get("diggable") is not False for block in group_blocks),
                    "harvest_tools": nearest.get("harvest_tools") or [],
                    "growth_stage": growth,
                    "mature": growth >= max_growth if growth is not None and max_growth is not None else None,
                }
            )
            group_index += 1
    facts.sort(
        key=lambda fact: (_distance_or_inf(fact.get("nearest_distance")), fact["group_id"])
    )
    return facts


def _container_facts(
    blocks: list[dict[str, Any]],
    self_position: dict[str, float] | None,
    options: WorldRuleOptions,
    provider: BlockEntityProvider | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = [block for block in blocks if _is_container(block["block_name"])]
    groups = _container_groups(candidates)
    facts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for group in groups:
        anchor = min(group, key=_distance_sort_key)
        positions = [block["position"] for block in group]
        distance = min(_distance_or_inf(block.get("distance")) for block in group)
        within_range = distance <= options.use_range
        line_of_sight = _combine_line_of_sight(
            [
                _has_line_of_sight(
                    self_position, _block_center(block["position"]), blocks
                )
                for block in group
            ]
        )
        fact: dict[str, Any] = {
            "container_type": anchor["block_name"],
            "position": anchor["position"],
            "physical_positions": positions,
            "distance": None if math.isinf(distance) else distance,
            "within_use_range": within_range,
            "line_of_sight": line_of_sight,
            "internal_status": "disabled",
            "slots": [],
            "totals": [],
            "empty_slots": None,
        }
        if not options.container_internal_enabled:
            facts.append(fact)
            continue
        if not within_range:
            fact["internal_status"] = "out_of_range"
            facts.append(fact)
            continue
        if options.require_line_of_sight and line_of_sight is False:
            fact["internal_status"] = "line_of_sight_blocked"
            facts.append(fact)
            continue
        if options.require_line_of_sight and line_of_sight is None:
            fact["internal_status"] = "query_failed"
            fact["reason"] = "line of sight unavailable"
            errors.append({"position": anchor["position"], "error": fact["reason"]})
            facts.append(fact)
            continue
        if provider is None:
            fact["internal_status"] = "query_failed"
            fact["reason"] = "block entity provider unavailable"
            errors.append({"position": anchor["position"], "error": fact["reason"]})
            facts.append(fact)
            continue
        try:
            reads = [
                provider.read_block_entity(block["position"], block["block_name"])
                for block in group
            ]
            slots: list[dict[str, Any]] = []
            for index, read in enumerate(reads):
                items = read.get("nbt", {}).get("Items", [])
                if not isinstance(items, list):
                    raise BlockEntityQueryError("block entity Items is not a list")
                slots.extend(_normalize_nbt_slots(items, index * 27, positions[index]))
            capacity = 27 * len(group)
            fact.update(
                {
                    "internal_status": "observed",
                    "slots": slots,
                    "totals": _aggregate_items(slots),
                    "empty_slots": max(0, capacity - len({slot["slot"] for slot in slots})),
                    "total_slots": capacity,
                    "sources": ["mineflayer_raw", "server_readonly"],
                    "freshness": "cached" if any(read.get("freshness") == "cached" for read in reads) else "live",
                    "observed_at_ms": min(
                        (read.get("observed_at_ms") for read in reads if read.get("observed_at_ms") is not None),
                        default=None,
                    ),
                    "age_ms": max((int(read.get("age_ms") or 0) for read in reads), default=0),
                }
            )
            query_errors = [read.get("query_error") for read in reads if read.get("query_error")]
            if query_errors:
                fact["query_warning"] = "; ".join(str(value) for value in query_errors)
        except Exception as exc:  # noqa: BLE001
            fact["internal_status"] = "query_failed"
            fact["reason"] = str(exc)
            errors.append({"position": anchor["position"], "error": str(exc)})
        facts.append(fact)
    facts.sort(key=_distance_sort_key)
    return facts, errors


def _processor_facts(
    blocks: list[dict[str, Any]],
    self_position: dict[str, float] | None,
    options: WorldRuleOptions,
    provider: BlockEntityProvider | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for block in blocks:
        if block["block_name"] not in _PROCESSOR_NAMES:
            continue
        distance = _number(block.get("distance"))
        within_range = distance is not None and distance <= options.use_range
        line_of_sight = _has_line_of_sight(
            self_position, _block_center(block["position"]), blocks
        )
        fact: dict[str, Any] = {
            "processor_type": block["block_name"],
            "position": block["position"],
            "distance": distance,
            "within_use_range": within_range,
            "line_of_sight": line_of_sight,
            "lit": (block.get("properties") or {}).get("lit"),
            "input": None,
            "fuel": None,
            "output": None,
            "burn_time_remaining_ticks": None,
            "cook_time_ticks": None,
            "cook_time_total_ticks": None,
            "progress_ratio": None,
            "internal_status": "disabled",
        }
        if not options.processor_internal_enabled:
            facts.append(fact)
            continue
        if not within_range:
            fact["internal_status"] = "out_of_range"
            facts.append(fact)
            continue
        if options.require_line_of_sight and line_of_sight is False:
            fact["internal_status"] = "line_of_sight_blocked"
            facts.append(fact)
            continue
        if options.require_line_of_sight and line_of_sight is None:
            fact["internal_status"] = "query_failed"
            fact["reason"] = "line of sight unavailable"
            errors.append({"position": block["position"], "error": fact["reason"]})
            facts.append(fact)
            continue
        if provider is None:
            fact["internal_status"] = "query_failed"
            fact["reason"] = "block entity provider unavailable"
            errors.append({"position": block["position"], "error": fact["reason"]})
            facts.append(fact)
            continue
        try:
            read = provider.read_block_entity(block["position"], block["block_name"])
            nbt = read.get("nbt") if isinstance(read.get("nbt"), dict) else {}
            slots = _normalize_nbt_slots(nbt.get("Items", []), 0, block["position"])
            by_slot = {entry["slot"]: entry for entry in slots}
            cook = _int_or_none(nbt.get("CookTime"))
            cook_total = _int_or_none(nbt.get("CookTimeTotal"))
            fact.update(
                {
                    "internal_status": "observed",
                    "input": by_slot.get(0),
                    "fuel": by_slot.get(1),
                    "output": by_slot.get(2),
                    "burn_time_remaining_ticks": _int_or_none(nbt.get("BurnTime")),
                    "cook_time_ticks": cook,
                    "cook_time_total_ticks": cook_total,
                    "progress_ratio": cook / cook_total if cook is not None and cook_total else None,
                    "sources": ["mineflayer_raw", "server_readonly"],
                    "freshness": read.get("freshness", "live"),
                    "observed_at_ms": read.get("observed_at_ms"),
                    "age_ms": int(read.get("age_ms") or 0),
                }
            )
            if read.get("query_error"):
                fact["query_warning"] = str(read["query_error"])
        except Exception as exc:  # noqa: BLE001
            fact["internal_status"] = "query_failed"
            fact["reason"] = str(exc)
            errors.append({"position": block["position"], "error": str(exc)})
        facts.append(fact)
    facts.sort(key=_distance_sort_key)
    return facts, errors


def _workstation_fact(
    block: dict[str, Any],
    self_position: dict[str, float] | None,
    self_yaw: float | None,
    options: WorldRuleOptions,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    distance = _number(block.get("distance"))
    return {
        "workstation_type": block["block_name"],
        "position": block["position"],
        "distance": distance,
        "within_use_range": distance is not None and distance <= options.use_range,
        "line_of_sight": _has_line_of_sight(
            self_position, _block_center(block["position"]), blocks
        ),
        "relative_direction": _relative_direction(
            self_position, self_yaw, _block_center(block["position"])
        ),
        "properties": block.get("properties") or {},
    }


def _environment_fact(raw: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    environment = raw.get("environment") if isinstance(raw.get("environment"), dict) else {}
    self_state = raw.get("self") if isinstance(raw.get("self"), dict) else {}
    time_state = environment.get("time") if isinstance(environment.get("time"), dict) else {}
    weather_state = environment.get("weather") if isinstance(environment.get("weather"), dict) else {}
    nearest = blocks[0] if blocks else {}
    time_of_day = _int_or_none(time_state.get("timeOfDay"))
    thunder_state = _number(weather_state.get("thunderState"))
    if not environment and not self_state and not nearest:
        return None
    return {
        "dimension": environment.get("dimension") or self_state.get("dimension"),
        "day_time": time_of_day,
        "day_count": _int_or_none(time_state.get("day")),
        "day_phase": _day_phase(time_of_day),
        "weather": _weather_name(weather_state),
        "thundering": thunder_state > 0 if thunder_state is not None else None,
        "difficulty": environment.get("difficulty"),
        "biome": _biome_name(nearest.get("biome")),
        "light": nearest.get("light"),
        "sky_light": nearest.get("sky_light"),
    }


def _coverage_fact(raw: dict[str, Any], enhancement_errors: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    counts = metadata.get("counts") if isinstance(metadata.get("counts"), dict) else {}
    return {
        "mode": metadata.get("mode"),
        "sub_mode": metadata.get("subMode"),
        "radius": metadata.get("radius"),
        "block_radius": metadata.get("blockRadius"),
        "fov_degrees": metadata.get("fovDegrees"),
        "observed_at_ms": _int_or_none(metadata.get("timestamp")),
        "entity_count": _int_or_none(counts.get("entities")),
        "player_count": _int_or_none(counts.get("players")),
        "item_count": _int_or_none(counts.get("items")),
        "block_count": _int_or_none(counts.get("blocks")),
        "scanned_blocks": _int_or_none(counts.get("scannedBlocks")),
        "air_blocks": _int_or_none(counts.get("airBlocks")),
        "unloaded_blocks": _int_or_none(counts.get("unloadedBlocks")),
        "enhancement_errors": enhancement_errors,
    }


def _normalize_nbt_slots(
    entries: Any,
    offset: int,
    physical_position: dict[str, int],
) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise BlockEntityQueryError("Items is not a list")
    slots: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BlockEntityQueryError("Items entry is not a compound")
        slot = _int_or_none(entry.get("Slot"))
        item_id = entry.get("id")
        count = _int_or_none(entry.get("Count"))
        if slot is None or not isinstance(item_id, str) or count is None:
            raise BlockEntityQueryError("Items entry lacks Slot, id, or Count")
        nbt = entry.get("tag")
        slots.append(
            {
                "id": item_id if ":" in item_id else f"minecraft:{item_id}",
                "name": _strip_namespace(item_id),
                "count": count,
                "slot": offset + slot,
                "physical_slot": slot,
                "physical_position": dict(physical_position),
                "nbt": nbt,
                "nbt_fingerprint": _nbt_fingerprint(nbt),
            }
        )
    slots.sort(key=lambda item: item["slot"])
    return slots


def _normalize_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_name = value.get("name") or value.get("id")
    if not raw_name:
        return None
    name = _strip_namespace(str(raw_name))
    max_durability = _int_or_none(value.get("maxDurability"))
    used = _int_or_none(value.get("durabilityUsed"))
    nbt = value.get("nbt")
    return {
        "id": str(raw_name) if ":" in str(raw_name) else f"minecraft:{raw_name}",
        "name": name,
        "identifier": value.get("identifier"),
        "owner": value.get("owner"),
        "display_name": value.get("displayName"),
        "count": _int_or_none(value.get("count")),
        "slot": _int_or_none(value.get("slot")),
        "max_stack_size": _int_or_none(value.get("stackSize")),
        "durability": max_durability - used if max_durability is not None and used is not None else None,
        "max_durability": max_durability,
        "nbt": nbt,
        "nbt_fingerprint": _nbt_fingerprint(nbt),
        "enchantments": value.get("enchantments") or _extract_enchantments(nbt),
    }


def _aggregate_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        key = (
            str(item["name"]),
            item.get("nbt_fingerprint"),
            item.get("identifier"),
        )
        if key not in totals:
            totals[key] = {
                "name": item["name"],
                "count": 0,
                "nbt": item.get("nbt"),
                "nbt_fingerprint": item.get("nbt_fingerprint"),
                "identifier": item.get("identifier"),
                "owner": item.get("owner"),
                "enchantments": item.get("enchantments") or [],
            }
        totals[key]["count"] += int(item.get("count") or 0)
    return sorted(
        totals.values(),
        key=lambda item: (
            item["name"],
            item.get("identifier") or "",
            item.get("nbt_fingerprint") or "",
        ),
    )


def _extract_enchantments(nbt: Any) -> list[dict[str, Any]]:
    if not isinstance(nbt, dict):
        return []
    raw = nbt.get("Enchantments") or nbt.get("ench") or []
    if not isinstance(raw, list):
        return []
    result = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        enchantment_id = entry.get("id")
        if isinstance(enchantment_id, str):
            enchantment_id = enchantment_id.removeprefix("minecraft:")
        result.append({"id": enchantment_id, "level": entry.get("lvl")})
    return result


def _nbt_fingerprint(nbt: Any) -> str | None:
    if nbt is None:
        return None
    encoded = json.dumps(nbt, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _container_groups(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    remaining = {(_position_tuple(block.get("position")), block["block_name"]): block for block in candidates}
    groups: list[list[dict[str, Any]]] = []
    while remaining:
        key = min(remaining, key=lambda value: (value[0] or (0, 0, 0), value[1]))
        block = remaining.pop(key)
        position = _position_tuple(block.get("position"))
        group = [block]
        properties = block.get("properties") or {}
        chest_type = properties.get("type")
        facing = properties.get("facing")
        if (
            position is not None
            and block["block_name"] in {"chest", "trapped_chest"}
            and chest_type in {"left", "right"}
        ):
            for neighbor in _horizontal_neighbors(position):
                neighbor_key = (neighbor, block["block_name"])
                candidate = remaining.get(neighbor_key)
                candidate_properties = candidate.get("properties") if candidate else {}
                if (
                    candidate
                    and isinstance(candidate_properties, dict)
                    and candidate_properties.get("type") in {"left", "right"}
                    and candidate_properties.get("type") != chest_type
                    and candidate_properties.get("facing") == facing
                ):
                    group.append(remaining.pop(neighbor_key))
                    break
        groups.append(group)
    return groups


def _enhancement_sources(facts: list[dict[str, Any]]) -> list[str]:
    sources = ["mineflayer_raw", "derived"]
    if any("server_readonly" in (fact.get("sources") or []) for fact in facts):
        sources.append("server_readonly")
    return sources


def _is_container(name: str) -> bool:
    return name in {"chest", "trapped_chest", "barrel"} or name.endswith("_shulker_box")


def _is_resource(name: str) -> bool:
    return name in _RESOURCE_EXACT or name in _MINERAL_BLOCKS or name.endswith(_RESOURCE_SUFFIXES)


def _is_projectile(entity: dict[str, Any], name: str) -> bool:
    return name in _PROJECTILE_NAMES or str(entity.get("type") or "").lower() == "projectile"


def _is_hostile(entity: dict[str, Any], name: str) -> bool:
    if name in _NEUTRAL_NAMES or name in _UTILITY_NAMES:
        return False
    return str(entity.get("type") or "").lower() == "hostile" or "hostile" in str(entity.get("kind") or "").lower()


def _is_living_entity(entity: dict[str, Any]) -> bool:
    entity_type = str(entity.get("type") or "").lower()
    kind = str(entity.get("kind") or "").lower()
    return entity_type in {"animal", "ambient", "water_creature", "mob", "passive"} or "mobs" in kind


def _passive_class(entity: dict[str, Any], name: str) -> str:
    if name in _UTILITY_NAMES:
        return "utility"
    if name in _NEUTRAL_NAMES or str(entity.get("type") or "").lower() == "neutral":
        return "neutral"
    return "passive"


def _is_dropped_item_entity(entity: dict[str, Any]) -> bool:
    return _entity_name(entity) in {"item", "item_stack"} or entity.get("droppedItem") is not None


def _entity_name(entity: dict[str, Any]) -> str:
    return _strip_namespace(
        str(entity.get("name") or entity.get("mobType") or entity.get("displayName") or "unknown")
    ).lower().replace(" ", "_")


def _moving_toward_self(
    entity: dict[str, Any], self_position: dict[str, float] | None
) -> bool | None:
    position = _position(entity.get("position"))
    velocity = _vector(entity.get("velocity"))
    if not position or not velocity or not self_position:
        return None
    relative = {axis: position[axis] - self_position[axis] for axis in "xyz"}
    return sum(relative[axis] * velocity[axis] for axis in "xyz") < -0.01


def _movement_state(velocity: dict[str, float] | None) -> str | None:
    if velocity is None:
        return None
    speed = math.sqrt(sum(value * value for value in velocity.values()))
    return "stationary" if speed < 0.03 else "moving"


def _relative_direction(
    origin: dict[str, float] | None,
    yaw: float | None,
    target: dict[str, float] | None,
) -> str | None:
    if origin is None or target is None or yaw is None:
        return None
    dx = target["x"] - origin["x"]
    dz = target["z"] - origin["z"]
    dy = target["y"] - origin["y"]
    horizontal = math.hypot(dx, dz)
    if horizontal < 0.001:
        if dy > 0.5:
            return "above"
        if dy < -0.5:
            return "below"
        return "same_position"
    bearing = math.atan2(-dx, -dz)
    delta = (bearing - yaw + math.pi) % (2 * math.pi) - math.pi
    index = int(round(delta / (math.pi / 4))) % 8
    return (
        "front",
        "front_left",
        "left",
        "back_left",
        "back",
        "back_right",
        "right",
        "front_right",
    )[index]


def _has_line_of_sight(
    origin: dict[str, float] | None,
    target: dict[str, float] | None,
    blocks: list[dict[str, Any]],
) -> bool | None:
    if origin is None or target is None:
        return None
    start = {"x": origin["x"], "y": origin["y"] + 1.62, "z": origin["z"]}
    dx, dy, dz = (target[axis] - start[axis] for axis in "xyz")
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance < 0.001:
        return True
    solid = {
        position
        for block in blocks
        if block.get("transparent") is False
        and block.get("bounding_box") not in {"empty", None}
        and (position := _position_tuple(block.get("position"))) is not None
    }
    target_block = (math.floor(target["x"]), math.floor(target["y"]), math.floor(target["z"]))
    steps = max(1, int(math.ceil(distance / 0.2)))
    for index in range(1, steps):
        ratio = index / steps
        current = (
            math.floor(start["x"] + dx * ratio),
            math.floor(start["y"] + dy * ratio),
            math.floor(start["z"] + dz * ratio),
        )
        if current != target_block and current in solid:
            return False
    return True


def _combine_line_of_sight(values: Iterable[bool | None]) -> bool | None:
    observed = list(values)
    if any(value is True for value in observed):
        return True
    if any(value is False for value in observed):
        return False
    return None


def _day_phase(time_of_day: int | None) -> str | None:
    if time_of_day is None:
        return None
    value = time_of_day % 24000
    if value < 1000 or value >= 23000:
        return "dawn"
    if value < 12000:
        return "day"
    if value < 14000:
        return "dusk"
    return "night"


def _weather_name(weather: dict[str, Any]) -> str | None:
    thunder = _number(weather.get("thunderState"))
    if thunder is not None and thunder > 0:
        return "thunder"
    rain = weather.get("isRaining")
    rain_state = _number(weather.get("rainState"))
    if rain or (rain_state is not None and rain_state > 0):
        return "rain"
    if rain is False or rain_state is not None or thunder is not None:
        return "clear"
    return None


def _biome_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("displayName") or value.get("id")
    return value


def _distance_sort_key(value: dict[str, Any]) -> tuple[float, str]:
    distance = _distance_or_inf(value.get("distance"))
    identity = value.get("entity_id") or value.get("group_id") or value.get("block_name") or ""
    return (distance, str(identity))


def _distance_or_inf(value: Any) -> float:
    distance = _number(value)
    return distance if distance is not None else math.inf


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _is_spectator_game_mode(value: Any) -> bool:
    if value == 3:
        return True
    return isinstance(value, str) and value.strip().lower() in {"3", "spectator"}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    values = {axis: _number(value.get(axis)) for axis in "xyz"}
    if any(item is None for item in values.values()):
        return None
    return {axis: float(values[axis]) for axis in "xyz"}  # type: ignore[arg-type]


def _block_position(value: Any) -> dict[str, int] | None:
    position = _position(value)
    if position is None:
        return None
    return {axis: math.floor(position[axis]) for axis in "xyz"}


def _vector(value: Any) -> dict[str, float] | None:
    return _position(value)


def _position_tuple(value: Any) -> tuple[int, int, int] | None:
    position = _block_position(value)
    if position is None:
        return None
    return position["x"], position["y"], position["z"]


def _tuple_position(value: tuple[int, int, int]) -> dict[str, int]:
    return {"x": value[0], "y": value[1], "z": value[2]}


def _block_center(value: Any) -> dict[str, float] | None:
    position = _block_position(value)
    if position is None:
        return None
    return {axis: position[axis] + 0.5 for axis in "xyz"}


def _face_neighbors(value: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    x, y, z = value
    return (
        (x + 1, y, z),
        (x - 1, y, z),
        (x, y + 1, z),
        (x, y - 1, z),
        (x, y, z + 1),
        (x, y, z - 1),
    )


def _horizontal_neighbors(value: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    x, y, z = value
    return ((x + 1, y, z), (x - 1, y, z), (x, y, z + 1), (x, y, z - 1))


def _strip_namespace(value: str) -> str:
    return value.split(":", 1)[-1]


__all__ = [
    "WORLD_SCHEMA_VERSION",
    "WORLD_SECTION_NAMES",
    "WorldRuleOptions",
    "WorldSnapshotRule",
    "world_snapshot",
    "select_world_sections",
]
