"""Seeded PVP agent spawn placement."""

from __future__ import annotations

from copy import deepcopy
import random
from typing import Any, Mapping, Sequence

from src.tasks.monster_clearance import EncounterBounds
from src.tasks.spec import TaskSpec, TaskSpecError


DEFAULT_PVP_AGENT_SPAWN_SPACING = 20.0
_SPAWN_SEED_SALT = 0x50565053
_PLACEMENT_ATTEMPTS_PER_AGENT = 512


class PVPSpawnError(ValueError):
    """Invalid or impossible PVP agent spawn placement."""


def pvp_task_data_with_seeded_spawns(
    data: Mapping[str, Any],
    *,
    seed: int | None = None,
    min_spacing: float = DEFAULT_PVP_AGENT_SPAWN_SPACING,
) -> dict[str, Any]:
    """Return PVP task data with deterministic scattered agent spawn points."""
    result = deepcopy(dict(data))
    if result.get("category") != "pvp":
        raise PVPSpawnError("seeded PVP spawn placement only supports category=pvp")
    params = result.get("params") if isinstance(result.get("params"), dict) else {}
    bounds = EncounterBounds.from_dict(params.get("bounds"))
    seed_value = _generation_seed(result, seed)
    teams = _team_members(result)
    positions = generate_agent_spawn_positions(
        teams=teams,
        bounds=bounds,
        seed=seed_value,
        min_spacing=min_spacing,
    )
    agent_order = _agent_names(result)
    initial_state = deepcopy(result.get("initial_state") or {})
    by_agent = _merge_by_agent_positions(
        initial_state.get("by_agent"),
        agent_order,
        positions,
    )
    initial_state["per_agent"] = dict(initial_state.get("per_agent") or {})
    initial_state["by_agent"] = by_agent
    result["initial_state"] = initial_state
    metadata = dict(result.get("metadata") or {})
    metadata["spawn_generation_seed"] = seed_value
    metadata["spawn_min_spacing"] = min_spacing
    result["metadata"] = metadata
    return result


def pvp_spec_with_seeded_spawns(
    spec: TaskSpec,
    *,
    seed: int | None = None,
    min_spacing: float = DEFAULT_PVP_AGENT_SPAWN_SPACING,
) -> TaskSpec:
    """Return a copy of a PVP spec whose agent positions are seed-generated."""
    try:
        data = pvp_task_data_with_seeded_spawns(
            spec.to_dict(),
            seed=seed,
            min_spacing=min_spacing,
        )
        result = TaskSpec.from_dict(data)
    except (PVPSpawnError, TaskSpecError) as exc:
        raise TaskSpecError(f"pvp 出生点生成失败: {exc}") from exc
    result._source_path = spec.source_path
    return result


def generate_agent_spawn_positions(
    *,
    teams: Sequence[Sequence[str]],
    bounds: EncounterBounds,
    seed: int,
    min_spacing: float = DEFAULT_PVP_AGENT_SPAWN_SPACING,
) -> dict[str, tuple[int, int, int]]:
    """Generate deterministic agent positions with pairwise 2D spacing."""
    if min_spacing < 0:
        raise PVPSpawnError("min_spacing 不能为负数")
    names = _interleaved_names(teams)
    if not names:
        raise PVPSpawnError("PVP 队伍不能为空")
    if len(set(names)) != len(names):
        raise PVPSpawnError("PVP Agent 名称不能重复")
    rng = random.Random(int(seed) ^ _SPAWN_SEED_SALT)
    positions: dict[str, tuple[int, int, int]] = {}
    existing: list[tuple[int, int, int]] = []
    min_distance_sq = float(min_spacing) * float(min_spacing)
    for name in names:
        position = _choose_spawn_position(
            bounds,
            rng,
            existing,
            min_distance_sq=min_distance_sq,
        )
        positions[name] = position
        existing.append(position)
    return positions


def _generation_seed(data: Mapping[str, Any], override: int | None) -> int:
    if override is not None:
        return _int_seed(override, "seed")
    params = data.get("params") if isinstance(data.get("params"), Mapping) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    environment = (
        data.get("environment") if isinstance(data.get("environment"), Mapping) else {}
    )
    for label, value in (
        ("params.generation_seed", params.get("generation_seed")),
        ("metadata.generation_seed", metadata.get("generation_seed")),
        ("environment.seed", environment.get("seed")),
    ):
        if value is not None:
            return _int_seed(value, label)
    return 0


def _int_seed(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PVPSpawnError(f"{label} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PVPSpawnError(f"{label} 必须是整数") from exc


def _team_members(data: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    agents = data.get("agents") if isinstance(data.get("agents"), Mapping) else {}
    teams = agents.get("teams") if isinstance(agents.get("teams"), list) else []
    result: list[tuple[str, ...]] = []
    for index, team in enumerate(teams):
        if not isinstance(team, Mapping):
            raise PVPSpawnError(f"agents.teams[{index}] 必须是映射")
        raw = team.get("agents") or team.get("members") or []
        if not isinstance(raw, list):
            raise PVPSpawnError(f"agents.teams[{index}].agents 必须是列表")
        members = tuple(str(name) for name in raw if str(name))
        if not members:
            raise PVPSpawnError(f"agents.teams[{index}] 不能为空")
        result.append(members)
    if len(result) != 2:
        raise PVPSpawnError("pvp 要求 agents.teams 恰好配置两支队伍")
    return tuple(result)


def _agent_names(data: Mapping[str, Any]) -> list[str]:
    agents = data.get("agents") if isinstance(data.get("agents"), Mapping) else {}
    names = agents.get("names") if isinstance(agents.get("names"), list) else []
    return [str(name) for name in names if str(name)]


def _interleaved_names(teams: Sequence[Sequence[str]]) -> list[str]:
    result: list[str] = []
    max_size = max((len(team) for team in teams), default=0)
    for index in range(max_size):
        for team in teams:
            if index < len(team):
                result.append(str(team[index]))
    return result


def _merge_by_agent_positions(
    current: Any,
    agent_order: Sequence[str],
    positions: Mapping[str, tuple[int, int, int]],
) -> list[dict[str, Any]]:
    previous: dict[str, dict[str, Any]] = {}
    if isinstance(current, list):
        for block in current:
            if isinstance(block, Mapping) and block.get("name"):
                previous[str(block["name"])] = deepcopy(dict(block))
    result: list[dict[str, Any]] = []
    for name in agent_order:
        if name not in positions:
            continue
        block = previous.get(name, {"name": name})
        x, y, z = positions[name]
        block["name"] = name
        block["position"] = {"x": x, "y": y, "z": z}
        result.append(block)
    return result


def _choose_spawn_position(
    bounds: EncounterBounds,
    rng: random.Random,
    existing: Sequence[tuple[int, int, int]],
    *,
    min_distance_sq: float,
) -> tuple[int, int, int]:
    if not existing:
        return _random_spawn_position(bounds, rng)
    best: tuple[int, int, int] | None = None
    best_score = -1.0
    for _ in range(_PLACEMENT_ATTEMPTS_PER_AGENT):
        candidate = _random_spawn_position(bounds, rng)
        score = _nearest_distance_sq(candidate, existing)
        if score >= min_distance_sq and score > best_score:
            best = candidate
            best_score = score
    if best is not None:
        return best
    return _choose_spawn_position_exhaustive(bounds, rng, existing, min_distance_sq)


def _choose_spawn_position_exhaustive(
    bounds: EncounterBounds,
    rng: random.Random,
    existing: Sequence[tuple[int, int, int]],
    min_distance_sq: float,
) -> tuple[int, int, int]:
    best: list[tuple[int, int, int]] = []
    best_score = -1.0
    for x in range(bounds.min_x, bounds.max_x + 1):
        for z in range(bounds.min_z, bounds.max_z + 1):
            candidate = (x, bounds.min_y, z)
            score = _nearest_distance_sq(candidate, existing)
            if score < min_distance_sq:
                continue
            if score > best_score:
                best = [candidate]
                best_score = score
            elif score == best_score:
                best.append(candidate)
    if not best:
        raise PVPSpawnError("场地无法容纳满足最小间距的 PVP 出生点")
    return best[rng.randrange(len(best))]


def _random_spawn_position(
    bounds: EncounterBounds,
    rng: random.Random,
) -> tuple[int, int, int]:
    return (
        rng.randint(bounds.min_x, bounds.max_x),
        bounds.min_y,
        rng.randint(bounds.min_z, bounds.max_z),
    )


def _nearest_distance_sq(
    candidate: tuple[int, int, int],
    existing: Sequence[tuple[int, int, int]],
) -> float:
    return min(_distance_sq_2d(candidate, position) for position in existing)


def _distance_sq_2d(
    left: tuple[int, int, int],
    right: tuple[int, int, int],
) -> float:
    return float((left[0] - right[0]) ** 2 + (left[2] - right[2]) ** 2)


__all__ = [
    "DEFAULT_PVP_AGENT_SPAWN_SPACING",
    "PVPSpawnError",
    "generate_agent_spawn_positions",
    "pvp_spec_with_seeded_spawns",
    "pvp_task_data_with_seeded_spawns",
]
