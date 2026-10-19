"""Task-level perception profiles composed from W01-W16 world facts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .base import RuleContext
from .composition import RuleGroup


TASK_SCHEMA_VERSION = "task_perception.v1"

_MINING_ITEMS = frozenset(
    {
        "lapis_block",
        "iron_block",
        "gold_block",
        "emerald_block",
        "diamond_block",
        "netherite_block",
    }
)
_MONSTER_KINDS = frozenset({"zombie", "skeleton"})
_MONSTER_SUPPLIES = frozenset(
    {
        "arrow",
        "splash_potion",
        "bread",
        "iron_sword",
        "iron_helmet",
        "iron_chestplate",
        "iron_leggings",
        "iron_boots",
    }
)
_PVP_SUPPLIES = frozenset(
    {
        "wooden_sword",
        "stone_sword",
        "golden_sword",
        "iron_sword",
        "diamond_sword",
        "netherite_sword",
        "bow",
        "arrow",
        "leather_helmet",
        "leather_chestplate",
        "leather_leggings",
        "leather_boots",
        "chainmail_helmet",
        "chainmail_chestplate",
        "chainmail_leggings",
        "chainmail_boots",
        "golden_helmet",
        "golden_chestplate",
        "golden_leggings",
        "golden_boots",
        "iron_helmet",
        "iron_chestplate",
        "iron_leggings",
        "iron_boots",
        "diamond_helmet",
        "diamond_chestplate",
        "diamond_leggings",
        "diamond_boots",
        "netherite_helmet",
        "netherite_chestplate",
        "netherite_leggings",
        "netherite_boots",
        "potion",
        "bread",
        "cooked_beef",
        "apple",
        "golden_apple",
    }
)
_WORKSTATION_BLOCKS = frozenset(
    {
        "crafting_table",
        "furnace",
        "blast_furnace",
        "smoker",
        "brewing_stand",
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
)


@dataclass(frozen=True)
class TaskPerceptionProfile:
    """Static world-section selection and output limits for one task category."""

    category: str
    sections: tuple[str, ...]
    limits: Mapping[str, int]


TASK_PERCEPTION_PROFILES: dict[str, TaskPerceptionProfile] = {
    "boss_combat": TaskPerceptionProfile(
        category="boss_combat",
        sections=(
            "self_state",
            "held_and_equipment",
            "inventory",
            "players",
            "hostile_entities",
        ),
        limits={"players": 4, "hostile_entities": 8},
    ),
    "mining": TaskPerceptionProfile(
        category="mining",
        sections=(
            "self_state",
            "held_and_equipment",
            "inventory",
            "players",
            "dropped_items",
            "nearby_blocks",
        ),
        limits={"players": 3, "dropped_items": 8, "nearby_blocks": 12},
    ),
    "monster_clearance": TaskPerceptionProfile(
        category="monster_clearance",
        sections=(
            "self_state",
            "held_and_equipment",
            "inventory",
            "players",
            "hostile_entities",
            "dropped_items",
        ),
        limits={
            "players": 3,
            "hostile_entities": 12,
            "dropped_items": 8,
        },
    ),
    "pvp": TaskPerceptionProfile(
        category="pvp",
        sections=(
            "self_state",
            "held_and_equipment",
            "inventory",
            "players",
            "dropped_items",
        ),
        limits={
            "players": 12,
            "dropped_items": 12,
        },
    ),
    "trade": TaskPerceptionProfile(
        category="trade",
        sections=(
            "self_state",
            "inventory",
            "players",
            "containers",
            "workstations",
        ),
        limits={"players": 3, "containers": 8, "workstations": 4},
    ),
}


@dataclass(frozen=True)
class TaskPerceptionRule:
    """Select and filter world facts, then add task-manager progress facts."""

    spec: Any
    metrics: Mapping[str, Any]
    elapsed_seconds: float
    registry_path: Path | None = None
    name: str = "task_perception"

    def apply(self, data: Any, context: RuleContext) -> dict[str, Any]:
        if not isinstance(data, dict) or not isinstance(data.get("sections"), dict):
            raise TypeError("task_perception expects a world perception snapshot")
        category = str(getattr(self.spec, "category", "") or "")
        profile = profile_for_category(category)
        spec_data = _spec_data(self.spec)
        registry = _load_registry(self.registry_path) if category == "trade" else []
        source_sections = data["sections"]
        selected: dict[str, Any] = {}
        for section_name in profile.sections:
            section = source_sections.get(section_name)
            if not isinstance(section, dict):
                continue
            selected[section_name] = _filter_section(
                section_name,
                section,
                category=category,
                spec=spec_data,
                agent_name=context.agent_name,
                world_sections=source_sections,
                registry=registry,
                limit=profile.limits.get(section_name),
            )

        self_position = _self_position(source_sections)
        progress = _task_progress(
            category=category,
            spec=spec_data,
            metrics=dict(self.metrics),
            elapsed_seconds=max(0.0, float(self.elapsed_seconds)),
            agent_name=context.agent_name,
            self_position=self_position,
            registry=registry,
        )
        if category == "boss_combat":
            _inject_boss_fact(
                selected.get("hostile_entities"),
                progress,
                self_position,
                spec_data,
            )
        elif category == "mining":
            _inject_mining_groups(selected.get("nearby_blocks"), progress)
            progress.pop("ore_groups", None)
        if category == "pvp":
            _annotate_pvp_self_state(selected, progress)
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "world_schema_version": data.get("schema_version"),
            "task_id": str(getattr(self.spec, "task_id", "") or spec_data.get("task_id") or ""),
            "category": category,
            "agent_name": context.agent_name,
            "sections": selected,
            "task": {
                "status": "observed",
                "sources": ["task_spec", "task_manager"],
                "freshness": "live",
                "facts": [progress],
                "summary": {
                    "profile_sections": list(profile.sections),
                    "limits": dict(profile.limits),
                },
            },
            "metadata": {
                "profile": category,
                "source_metadata": deepcopy(data.get("metadata") or {}),
            },
        }


def profile_for_category(category: str) -> TaskPerceptionProfile:
    try:
        return TASK_PERCEPTION_PROFILES[category]
    except KeyError as exc:
        raise ValueError(f"unsupported task perception category: {category}") from exc


def task_perception(
    spec: Any,
    *,
    metrics: Mapping[str, Any] | None = None,
    elapsed_seconds: float = 0.0,
    registry_path: str | Path | None = None,
) -> RuleGroup:
    """Build the registered task profile as one reusable perception rule group."""

    category = str(getattr(spec, "category", "") or "")
    profile = profile_for_category(category)
    active_path = Path(registry_path) if registry_path is not None else None
    return RuleGroup(
        f"task_perception.{category}",
        TaskPerceptionRule(
            spec=spec,
            metrics=dict(metrics or {}),
            elapsed_seconds=elapsed_seconds,
            registry_path=active_path,
        ),
        {
            "category": category,
            "sections": list(profile.sections),
            "limits": dict(profile.limits),
        },
    )


def _filter_section(
    section_name: str,
    section: Mapping[str, Any],
    *,
    category: str,
    spec: Mapping[str, Any],
    agent_name: str | None,
    world_sections: Mapping[str, Any],
    registry: list[dict[str, Any]],
    limit: int | None,
) -> dict[str, Any]:
    result = deepcopy(dict(section))
    source_facts = [fact for fact in section.get("facts", []) if isinstance(fact, dict)]
    facts = source_facts
    if section_name == "players":
        names = set((spec.get("agents") or {}).get("names") or [])
        facts = [fact for fact in facts if not names or fact.get("player_name") in names]
        if category == "pvp":
            facts = _annotate_pvp_players(facts, spec, agent_name)
    elif section_name == "inventory":
        facts = [_filter_inventory_fact(fact, category) for fact in facts]
    elif section_name == "hostile_entities":
        if category == "monster_clearance":
            facts = [fact for fact in facts if _plain_name(fact.get("kind")) in _MONSTER_KINDS]
        facts = sorted(facts, key=lambda fact: _hostile_sort_key(fact, category, spec))
    elif section_name == "dropped_items":
        if category == "mining":
            allowed = _MINING_ITEMS
        elif category == "pvp":
            allowed = _PVP_SUPPLIES
        else:
            allowed = _MONSTER_SUPPLIES
        facts = [fact for fact in facts if _plain_name(_item_name(fact.get("item"))) in allowed]
    elif section_name == "resources":
        facts = [fact for fact in facts if _plain_name(fact.get("resource_name")) in _MINING_ITEMS]
    elif section_name == "nearby_blocks":
        self_position = _self_position(world_sections)
        facts = [fact for fact in facts if _is_task_obstacle(fact, self_position)]
        facts.sort(key=_distance_sort_key)
    elif section_name == "containers":
        facts = _trade_containers(facts, registry, spec, agent_name)
    elif section_name == "workstations" and category == "trade":
        crafting = _trade_crafting_position(spec)
        if crafting is not None:
            facts = [fact for fact in facts if _position_key(fact.get("position")) == crafting]

    source_count = len(facts)
    if limit is not None:
        facts = facts[:limit]
    result["facts"] = facts
    if result.get("status") in {"observed", "empty"}:
        result["status"] = "observed" if facts else "empty"
    summary = deepcopy(result.get("summary") or {})
    summary.update(
        {
            "source_fact_count": len(source_facts),
            "filtered_fact_count": source_count,
            "returned_fact_count": len(facts),
            "omitted_fact_count": max(0, source_count - len(facts)),
        }
    )
    result["summary"] = summary
    return result


def _filter_inventory_fact(fact: dict[str, Any], category: str) -> dict[str, Any]:
    result = deepcopy(fact)
    if category == "trade":
        return result
    allowed = {
        "boss_combat": lambda name: (
            name.endswith("_sword")
            or name in {"bow", "arrow", "golden_apple", "splash_potion", "potion"}
        ),
        "mining": lambda name: name in _MINING_ITEMS,
        "monster_clearance": lambda name: (
            name.endswith("_sword")
            or name in {"bow", "potion"}
            or name in _MONSTER_SUPPLIES
        ),
        "pvp": lambda name: (
            name in _PVP_SUPPLIES
            or name.endswith("_sword")
            or name.endswith("_helmet")
            or name.endswith("_chestplate")
            or name.endswith("_leggings")
            or name.endswith("_boots")
        ),
    }[category]
    for key in ("totals", "slots"):
        values = result.get(key)
        if isinstance(values, list):
            result[key] = [
                item for item in values
                if isinstance(item, dict) and allowed(_plain_name(_item_name(item)))
            ]
    return result


def _trade_containers(
    facts: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    spec: Mapping[str, Any],
    agent_name: str | None,
) -> list[dict[str, Any]]:
    agent_names = set((spec.get("agents") or {}).get("names") or [])
    if not agent_names:
        agent_names = {"Alice", "Bob", "Carol", "Dave"}
    allowed: dict[str, tuple[str, str]] = {}
    for owner in agent_names:
        allowed[f"{owner}'s_box"] = (owner, "personal_box")
        allowed[f"{owner}'s_shop"] = (owner, "shop")
    registered: dict[tuple[int, int, int], dict[str, Any]] = {}
    for entry in registry:
        key = _position_key(entry.get("position"))
        name = entry.get("name")
        if key is not None and name in allowed:
            registered[key] = entry

    result: list[dict[str, Any]] = []
    for fact in facts:
        positions = fact.get("physical_positions") or [fact.get("position")]
        entry = next(
            (registered[key] for value in positions if (key := _position_key(value)) in registered),
            None,
        )
        if entry is None:
            continue
        logical_name = str(entry["name"])
        owner, role = allowed[logical_name]
        mapped = deepcopy(fact)
        mapped.update(
            {
                "registered_id": entry.get("id"),
                "registered_name": logical_name,
                "registered_position": deepcopy(entry.get("position")),
                "owner": owner,
                "container_role": role,
            }
        )
        if role == "personal_box" and owner != agent_name:
            _hide_container_internal(mapped, "access_denied")
        elif role == "shop" and mapped.get("internal_status") == "observed":
            mapped["sale_totals"] = [
                item for item in mapped.get("totals", [])
                if _plain_name(_item_name(item)) != "gold_nugget"
            ]
        result.append(mapped)
    result.sort(key=_distance_sort_key)
    return result


def _hide_container_internal(fact: dict[str, Any], status: str) -> None:
    fact["internal_status"] = status
    fact["slots"] = []
    fact["totals"] = []
    fact["empty_slots"] = None
    fact.pop("sale_totals", None)
    fact.pop("total_slots", None)
    fact.pop("freshness", None)
    fact.pop("observed_at_ms", None)
    fact.pop("age_ms", None)


def _task_progress(
    *,
    category: str,
    spec: Mapping[str, Any],
    metrics: dict[str, Any],
    elapsed_seconds: float,
    agent_name: str | None,
    self_position: tuple[float, float, float] | None,
    registry: list[dict[str, Any]],
) -> dict[str, Any]:
    completion = spec.get("completion") or {}
    timeout = float(completion.get("timeout_seconds") or 0.0)
    progress: dict[str, Any] = {
        "task_id": spec.get("task_id"),
        "category": category,
        "elapsed_seconds": elapsed_seconds,
        "remaining_seconds": max(0.0, timeout - elapsed_seconds),
    }
    if category == "boss_combat":
        success = completion.get("success") or {}
        progress.update(
            {
                "boss_type": (spec.get("params") or {}).get("boss_type"),
                "boss_uuid": metrics.get("boss_uuid"),
                "boss_entity_id": metrics.get("boss_entity_id"),
                "boss_position": deepcopy(metrics.get("boss_position")),
                "current_health": metrics.get("current_health"),
                "max_health": metrics.get("max_health"),
                "current_ratio": metrics.get("current_ratio"),
                "target_ratio": metrics.get("target_ratio", success.get("ratio")),
                "alive_agents": deepcopy(metrics.get("alive_agents") or []),
                "total_agents": metrics.get("total_agents", (spec.get("agents") or {}).get("count")),
                "query_error": metrics.get("query_error"),
            }
        )
    elif category == "mining":
        for key in (
            "current_value",
            "target_value",
            "item_counts",
            "item_values",
            "item_value_totals",
            "active_groups",
            "active_ore_groups",
            "commands_sent",
            "command_errors",
        ):
            progress[key] = deepcopy(metrics.get(key))
        progress["ore_groups"] = _manager_ore_groups(
            metrics.get("active_ore_groups"), self_position
        )
    elif category == "monster_clearance":
        for key in (
            "planned_monsters",
            "spawned_monsters",
            "alive_monsters",
            "eliminated_monsters",
            "monsters_by_type",
            "monsters_by_profile",
            "spawned_supplies",
            "alive_agents",
            "eliminated_agents",
            "schedule_complete",
            "spawn_results_verified",
            "commands_sent",
            "command_errors",
            "query_note",
        ):
            progress[key] = deepcopy(metrics.get(key))
    elif category == "pvp":
        for key in (
            "teams",
            "winner",
            "alive_agents",
            "eliminated_agents",
            "spawned_supplies",
            "spawned_supply_waves",
            "last_drop",
            "last_drop_batch",
            "sudden_death_active",
            "hunger_pressure_after_seconds",
            "hunger_interval_seconds",
            "out_of_bounds_interval_seconds",
            "out_of_bounds_damage",
            "bounds",
            "gear_probability",
            "drop_class_probabilities",
            "food_healing_disabled",
            "query_note",
        ):
            progress[key] = deepcopy(metrics.get(key))
    elif category == "trade":
        agent_metrics = (metrics.get("agents") or {}).get(agent_name, {})
        names = sorted(
            str(entry.get("name"))
            for entry in registry
            if str(entry.get("name") or "").endswith("'s_shop")
        )
        progress.update(
            {
                "agent": agent_name,
                "targets": deepcopy(agent_metrics.get("targets") or {}),
                "target_total_value": agent_metrics.get("target_total_value"),
                "complete": agent_metrics.get("complete"),
                "completed_agents": metrics.get("completed_agents"),
                "total_agents": metrics.get("total_agents"),
                "query_error": (metrics.get("query_errors") or {}).get(agent_name),
                "personal_box": f"{agent_name}'s_box" if agent_name else None,
                "own_shop": f"{agent_name}'s_shop" if agent_name else None,
                "shops": names,
            }
        )
    return progress


def _annotate_pvp_players(
    facts: list[dict[str, Any]],
    spec: Mapping[str, Any],
    agent_name: str | None,
) -> list[dict[str, Any]]:
    team_by_agent = _team_by_agent(spec)
    own_team = team_by_agent.get(str(agent_name)) if agent_name else None
    annotated: list[dict[str, Any]] = []
    for fact in facts:
        player = str(fact.get("player_name") or "")
        team = team_by_agent.get(player)
        mapped = deepcopy(fact)
        if team:
            mapped["team"] = team
        if team and own_team:
            if player == agent_name:
                mapped["relation"] = "self"
            else:
                mapped["relation"] = "friendly" if team == own_team else "enemy"
        annotated.append(mapped)
    return annotated


def _annotate_pvp_self_state(
    sections: dict[str, Any],
    progress: Mapping[str, Any],
) -> None:
    section = sections.get("self_state")
    if not isinstance(section, dict):
        return
    facts = section.get("facts")
    if not isinstance(facts, list) or not facts or not isinstance(facts[0], dict):
        return
    fact = facts[0]
    reasons: list[str] = []
    if _outside_pvp_bounds(fact.get("position"), progress.get("bounds")):
        reasons.append("out_of_bounds")
    if _pvp_hunger_pressure_active(progress):
        reasons.append("hunger_pressure")
    if reasons:
        fact["persistent_damage_state"] = True
        fact["persistent_damage_reasons"] = reasons


def _outside_pvp_bounds(position: Any, bounds: Any) -> bool:
    if not isinstance(position, Mapping) or not isinstance(bounds, Mapping):
        return False
    try:
        x = float(position["x"])
        z = float(position["z"])
        min_x = float(bounds["min_x"])
        max_x = float(bounds["max_x"])
        min_z = float(bounds["min_z"])
        max_z = float(bounds["max_z"])
    except (KeyError, TypeError, ValueError):
        return False
    return x < min_x or x > max_x or z < min_z or z > max_z


def _pvp_hunger_pressure_active(progress: Mapping[str, Any]) -> bool:
    if progress.get("sudden_death_active") is True:
        return True
    try:
        elapsed = float(progress.get("elapsed_seconds"))
        threshold = float(progress.get("hunger_pressure_after_seconds"))
    except (TypeError, ValueError):
        return False
    return threshold >= 0 and elapsed >= threshold


def _team_by_agent(spec: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    agents = spec.get("agents") or {}
    for team in agents.get("teams") or []:
        if not isinstance(team, Mapping):
            continue
        team_id = str(team.get("id") or team.get("name") or "")
        for name in team.get("agents") or team.get("members") or []:
            result[str(name)] = team_id
    return result


def _hostile_sort_key(
    fact: Mapping[str, Any], category: str, spec: Mapping[str, Any]
) -> tuple[int, float, str]:
    boss_type = _plain_name((spec.get("params") or {}).get("boss_type"))
    is_boss = category == "boss_combat" and (
        _plain_name(fact.get("kind")) == boss_type or "boss" in (fact.get("roles") or [])
    )
    return (0 if is_boss else 1, _distance_value(fact), str(fact.get("entity_id") or ""))


def _inject_boss_fact(
    section: Any,
    progress: Mapping[str, Any],
    self_position: tuple[float, float, float] | None,
    spec: Mapping[str, Any],
) -> None:
    """Keep the task Boss visible even when local entity perception omits it."""
    if not isinstance(section, dict):
        return
    boss_type = _plain_name((spec.get("params") or {}).get("boss_type"))
    if not boss_type:
        return
    facts = [fact for fact in section.get("facts") or [] if isinstance(fact, dict)]
    boss = next(
        (
            fact
            for fact in facts
            if "boss" in (fact.get("roles") or [])
            or _plain_name(fact.get("kind")) == boss_type
        ),
        None,
    )
    if boss is None:
        entity_id = _compact_entity_id(progress.get("boss_entity_id"))
        boss = {
            "entity_id": entity_id if entity_id is not None else "boss",
            "uuid": progress.get("boss_uuid"),
            "kind": boss_type,
            "entity_class": "hostile",
            "roles": ["boss"],
            "position": deepcopy(progress.get("boss_position")),
            "distance": _position_distance(
                _position_key(progress.get("boss_position")), self_position
            )
            if _position_key(progress.get("boss_position")) is not None
            else None,
            "health": progress.get("current_health"),
            "max_health": progress.get("max_health"),
            "equipment": {},
        }
        facts.append(boss)
    else:
        boss["kind"] = boss_type
        boss["roles"] = list(dict.fromkeys([*(boss.get("roles") or []), "boss"]))
        if progress.get("boss_position") is not None:
            boss["position"] = deepcopy(progress["boss_position"])
            position = _position_key(progress["boss_position"])
            boss["distance"] = _position_distance(position, self_position) if position else None
        boss["health"] = progress.get("current_health")
        boss["max_health"] = progress.get("max_health")
    facts.sort(key=lambda fact: _hostile_sort_key(fact, "boss_combat", spec))
    section["facts"] = facts[:8]
    if facts:
        section["status"] = "observed"


def _inject_mining_groups(section: Any, progress: Mapping[str, Any]) -> None:
    """Render manager-owned ore groups inside the mining nearby-block section."""
    if not isinstance(section, dict):
        return
    groups = []
    for group in progress.get("ore_groups") or []:
        if not isinstance(group, Mapping):
            continue
        fact = deepcopy(dict(group))
        fact.update(
            {
                "block_name": _plain_name(group.get("resource_name")),
                "position": deepcopy(group.get("anchor_position")),
                "is_ore_group": True,
            }
        )
        groups.append(fact)
    if not groups:
        return
    obstacles = [
        fact
        for fact in section.get("facts") or []
        if isinstance(fact, dict) and not fact.get("is_ore_group")
    ]
    groups.sort(key=_distance_sort_key)
    section["facts"] = (groups + obstacles)[:12]
    section["status"] = "observed"


def _is_task_obstacle(
    fact: Mapping[str, Any], self_position: tuple[float, float, float] | None
) -> bool:
    name = _plain_name(fact.get("block_name"))
    if name in _MINING_ITEMS or name in _WORKSTATION_BLOCKS:
        return False
    if name in {"chest", "trapped_chest", "barrel"} or name.endswith("_shulker_box"):
        return False
    distance = fact.get("distance")
    try:
        if distance is None or float(distance) > 6.0:
            return False
    except (TypeError, ValueError):
        return False
    if fact.get("bounding_box") in {None, "empty"} or fact.get("transparent") is True:
        return False
    position = _position_key(fact.get("position"))
    if position is None:
        return False
    if self_position is not None and position[1] < math.floor(self_position[1]):
        return False
    return True


def _self_position(sections: Mapping[str, Any]) -> tuple[float, float, float] | None:
    section = sections.get("self_state")
    if not isinstance(section, dict):
        return None
    facts = section.get("facts") or []
    if not facts or not isinstance(facts[0], dict):
        return None
    position = facts[0].get("position")
    if not isinstance(position, dict):
        return None
    try:
        return float(position["x"]), float(position["y"]), float(position["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _trade_crafting_position(spec: Mapping[str, Any]) -> tuple[int, int, int] | None:
    params = spec.get("params") or {}
    configured = params.get("crafting_table")
    if configured is not None:
        return _position_key(configured)
    return (1, 0, 0)


def _load_registry(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chests = payload.get("chests") if isinstance(payload, dict) else None
    if not isinstance(chests, list):
        return []
    return [deepcopy(entry) for entry in chests if isinstance(entry, dict)]


def _spec_data(spec: Any) -> dict[str, Any]:
    to_dict = getattr(spec, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    elif isinstance(spec, Mapping):
        value = dict(spec)
    else:
        raise TypeError("task perception spec must be a TaskSpec-like mapping")
    return deepcopy(value)


def _item_name(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("name") or value.get("id") or "")


def _plain_name(value: Any) -> str:
    return str(value or "").split(":", 1)[-1]


def _compact_entity_id(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    return None


def _position_key(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return int(value["x"]), int(value["y"]), int(value["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _manager_ore_groups(
    value: Any,
    self_position: tuple[float, float, float] | None,
) -> list[dict[str, Any]]:
    """Split each manager lifecycle group by its currently connected blocks."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for group in value:
        if not isinstance(group, Mapping):
            continue
        positions = {
            position
            for raw_position in group.get("positions") or []
            if (position := _position_key(raw_position)) is not None
        }
        block = _plain_name(group.get("block"))
        if not positions or not block:
            continue
        component_index = 0
        while positions:
            start = min(positions)
            positions.remove(start)
            component = [start]
            queue = [start]
            while queue:
                current = queue.pop()
                for neighbor in _face_neighbors(current):
                    if neighbor in positions:
                        positions.remove(neighbor)
                        queue.append(neighbor)
                        component.append(neighbor)
            anchor = min(
                component,
                key=lambda position: (
                    _position_distance(position, self_position),
                    position,
                ),
            )
            result.append(
                {
                    "group_id": f"{group.get('group_id', '')}#{component_index}",
                    "source_group_id": group.get("group_id"),
                    "resource_name": block,
                    "block": group.get("block"),
                    "block_count": len(component),
                    "positions": [
                        _tuple_position(position) for position in sorted(component)
                    ],
                    "anchor_position": _tuple_position(anchor),
                    "distance": _position_distance(anchor, self_position),
                }
            )
            component_index += 1
    result.sort(
        key=lambda group: (
            _distance_or_inf(group.get("distance")),
            str(group.get("source_group_id") or ""),
            str(group.get("group_id") or ""),
        )
    )
    return result


def _face_neighbors(position: tuple[int, int, int]) -> tuple[tuple[int, int, int], ...]:
    x, y, z = position
    return (
        (x + 1, y, z),
        (x - 1, y, z),
        (x, y + 1, z),
        (x, y - 1, z),
        (x, y, z + 1),
        (x, y, z - 1),
    )


def _position_distance(
    position: tuple[int, int, int],
    self_position: tuple[float, float, float] | None,
) -> float:
    if self_position is None:
        return math.inf
    x, y, z = position
    self_x, self_y, self_z = self_position
    return math.sqrt(
        (x + 0.5 - self_x) ** 2
        + (y + 0.5 - self_y) ** 2
        + (z + 0.5 - self_z) ** 2
    )


def _tuple_position(position: tuple[int, int, int]) -> dict[str, int]:
    return {"x": position[0], "y": position[1], "z": position[2]}


def _distance_or_inf(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.inf


def _distance_value(fact: Mapping[str, Any]) -> float:
    value = fact.get("distance", fact.get("nearest_distance"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.inf


def _distance_sort_key(fact: Mapping[str, Any]) -> tuple[float, str]:
    return _distance_value(fact), str(fact.get("registered_name") or fact.get("entity_id") or "")


__all__ = [
    "TASK_SCHEMA_VERSION",
    "TASK_PERCEPTION_PROFILES",
    "TaskPerceptionProfile",
    "TaskPerceptionRule",
    "profile_for_category",
    "task_perception",
]
