"""从 YAML 模板批量生成动态怪物清剿任务包。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import math
from pathlib import Path
import random
from typing import Any

import yaml

from src.tasks.spec import TaskSpec, TaskSpecError

from .schedule import (
    EncounterBounds,
    EncounterSpawnRecord,
    MonsterClearanceGenerationSpec,
    MonsterClearanceScheduleError,
    generate_spawn_schedule,
    write_command_file,
    write_spawn_schedule,
)


IRON_ARMOR_PIECES = (
    "minecraft:iron_helmet",
    "minecraft:iron_chestplate",
    "minecraft:iron_leggings",
    "minecraft:iron_boots",
)


@dataclass(frozen=True)
class TaskBundlePaths:
    directory: Path
    task: Path
    schedule: Path
    commands: Path


def load_generation_template(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MonsterClearanceScheduleError(
            f"无法解析 monster_clearance 模板 {target}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MonsterClearanceScheduleError("monster_clearance 模板顶层必须是映射")
    if not isinstance(data.get("task"), dict) or not isinstance(
        data.get("generation"), dict
    ):
        raise MonsterClearanceScheduleError("模板必须包含 task 和 generation 映射")
    if not data.get("task_id_prefix") or not data.get("name_prefix"):
        raise MonsterClearanceScheduleError("task_id_prefix 和 name_prefix 必填")
    return data


def generation_spec_from_template(
    template: dict[str, Any],
) -> MonsterClearanceGenerationSpec:
    generation = template["generation"]
    task = template["task"]
    completion = task.get("completion") or {}
    try:
        agent_positions = tuple(
            (
                int(item["position"]["x"]),
                int(item["position"]["y"]),
                int(item["position"]["z"]),
            )
            for item in task["initial_state"]["by_agent"]
        )
        monster_weights = tuple(
            (str(item["resource"]), float(item["weight"]))
            for item in generation["monsters"]
        )
        zombie_profile_weights = tuple(
            (str(item["profile"]), float(item["weight"]))
            for item in generation["zombie_profiles"]
        )
        supply_weights = tuple(
            (str(item["resource"]), float(item["weight"]))
            for item in generation["supply_weights"]
        )
        supply_amounts = tuple(
            (str(item["resource"]), int(item.get("amount", 1)))
            for item in generation["supply_weights"]
        )
        initial_bounds_data = generation.get("initial_spawn_bounds")
        initial_bounds = (
            EncounterBounds.from_dict(initial_bounds_data)
            if initial_bounds_data
            else None
        )
        return MonsterClearanceGenerationSpec(
            bounds=EncounterBounds.from_dict(generation["bounds"]),
            timeout_seconds=int(completion["timeout_seconds"]),
            monster_count=int(generation["monster_count"]),
            initial_monster_count=int(generation["initial_monster_count"]),
            batch_interval_seconds=int(generation["batch_interval_seconds"]),
            last_spawn_deadline_seconds=int(generation["last_spawn_deadline_seconds"]),
            monster_min_distance=float(generation["monster_min_distance"]),
            agent_positions=agent_positions,
            initial_agent_min_distance=float(
                generation["initial_agent_min_distance"]
            ),
            supply_drop_min_distance=float(generation["supply_drop_min_distance"]),
            supply_drop_max_distance=float(generation["supply_drop_max_distance"]),
            monster_weights=monster_weights,
            zombie_profile_weights=zombie_profile_weights,
            min_elite_zombies=int(generation["elite_zombies"]["min"]),
            max_elite_zombies=int(generation["elite_zombies"]["max"]),
            supply_weights=supply_weights,
            supply_amounts=supply_amounts,
            batch_size=int(generation.get("batch_size", 4)),
            initial_spawn_bounds=initial_bounds,
            supply_interval_seconds=int(generation.get("supply_interval_seconds", 5)),
            max_placement_attempts=int(generation.get("max_placement_attempts", 5000)),
            max_time_attempts=int(generation.get("max_time_attempts", 1000)),
            agent_spawn_center=tuple(
                float((task.get("environment") or {}).get("world_spawn", {}).get(axis, 0))
                for axis in ("x", "y", "z")
            ),
            agent_spawn_radius=float(generation.get("agent_spawn_radius", 10.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MonsterClearanceScheduleError(
            f"monster_clearance 生成参数非法: {exc}"
        ) from exc


def generate_task_batch(
    template_path: str | Path,
    *,
    count: int,
    base_seed: int,
    output_dir: str | Path,
    overwrite: bool = False,
) -> list[TaskBundlePaths]:
    """生成同一任务的多个编号初始化文件（config/<seed>.yaml 及配套资源）。"""
    if count < 1:
        raise ValueError("count 必须大于 0")
    template = load_generation_template(template_path)
    generation_spec = generation_spec_from_template(template)
    root = Path(output_dir)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    task_id = template["task_id_prefix"]
    bundles: list[TaskBundlePaths] = []
    for index in range(count):
        seed = base_seed + index
        task_path = config_dir / f"{seed}.yaml"
        schedule_path = config_dir / f"{seed}.schedule.txt"
        commands_path = config_dir / f"{seed}.commands.txt"
        if not overwrite and any(
            path.exists() for path in (task_path, schedule_path, commands_path)
        ):
            raise FileExistsError(f"输出文件已存在: {task_path}")
        by_agent = (template["task"].get("initial_state") or {}).get("by_agent") or []
        agent_positions = generate_agent_spawn_positions(
            generation_spec,
            seed,
            len(by_agent),
        )
        schedule_spec = replace(
            generation_spec,
            agent_positions=tuple(agent_positions),
        )
        records = generate_spawn_schedule(schedule_spec, seed)
        task_data = build_task_data(
            template,
            generation_spec,
            task_id=task_id,
            seed=seed,
            agent_positions=agent_positions,
        )
        try:
            TaskSpec.from_dict(task_data)
        except TaskSpecError as exc:
            raise MonsterClearanceScheduleError(f"生成任务规格非法: {exc}") from exc
        command_options = _command_options(task_data)
        write_spawn_schedule(records, schedule_path)
        write_command_file(records, commands_path, **command_options)
        task_path.write_text(
            yaml.safe_dump(task_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        bundles.append(
            TaskBundlePaths(
                directory=config_dir,
                task=task_path,
                schedule=schedule_path,
                commands=commands_path,
            )
        )
    return bundles


def build_task_data(
    template: dict[str, Any],
    generation_spec: MonsterClearanceGenerationSpec,
    *,
    task_id: str,
    seed: int,
    agent_positions: list[tuple[float, float, float]] | None = None,
) -> dict[str, Any]:
    task = deepcopy(template["task"])
    task.pop("description", None)
    task["task_id"] = task_id
    task["name"] = f"{template['name_prefix']} (seed {seed})"
    if agent_positions is not None:
        initial_state = task.get("initial_state") or {}
        by_agent = initial_state.get("by_agent") or []
        if len(by_agent) != len(agent_positions):
            raise MonsterClearanceScheduleError(
                "Agent 出生点数量与 initial_state.by_agent 不一致"
            )
        for block, position in zip(by_agent, agent_positions):
            block["position"] = {
                "x": position[0],
                "y": position[1],
                "z": position[2],
            }
    params = dict(task.get("params") or {})
    params.update(
        {
            "schedule_file": f"{seed}.schedule.txt",
            "command_file": f"{seed}.commands.txt",
            "bounds": generation_spec.bounds.to_dict(),
            "monster_count": generation_spec.monster_count,
            "initial_monster_count": generation_spec.initial_monster_count,
            "batch_size": generation_spec.batch_size,
            "batch_interval_seconds": generation_spec.batch_interval_seconds,
            "last_spawn_deadline_seconds": generation_spec.last_spawn_deadline_seconds,
            "monster_min_distance": generation_spec.monster_min_distance,
            "initial_agent_min_distance": (
                generation_spec.initial_agent_min_distance
            ),
            "agent_spawn_radius": generation_spec.agent_spawn_radius,
            "supply_interval_seconds": generation_spec.supply_interval_seconds,
            "supply_drop_min_distance": generation_spec.supply_drop_min_distance,
            "supply_drop_max_distance": generation_spec.supply_drop_max_distance,
            "supply_weights": _supply_weight_entries(generation_spec),
            "monster_tag": f"ac_mon_{seed}",
            "supply_tag": f"ac_sup_{seed}",
            "spawn_objective": "acTaskSpawn",
            "death_objective": "acTaskDeath",
            "generation_seed": seed,
        }
    )
    task["params"] = params
    metadata = dict(task.get("metadata") or {})
    metadata["generation_seed"] = seed
    task["metadata"] = metadata
    initial_state = deepcopy(task.get("initial_state") or {})
    per_agent = dict(initial_state.get("per_agent") or {})
    inventory = {
        "minecraft:stone_sword": 1,
        "minecraft:bow": 1,
        "minecraft:arrow": 16,
    }
    inventory.update({item: 1 for item in _random_iron_armor(seed)})
    per_agent["inventory"] = inventory
    per_agent.pop("equipment", None)
    initial_state["per_agent"] = per_agent
    task["initial_state"] = initial_state
    return task


def generate_agent_spawn_positions(
    spec: MonsterClearanceGenerationSpec,
    seed: int,
    count: int,
) -> list[tuple[float, float, float]]:
    """Generate distinct Agent positions inside the configured center radius."""
    if count < 1:
        raise MonsterClearanceScheduleError("Agent 数量必须大于 0")
    rng = random.Random(seed ^ 0x5EED5EED)
    center_x, center_y, center_z = spec.agent_spawn_center
    positions: list[tuple[float, float, float]] = []
    for _ in range(spec.max_placement_attempts):
        radius = math.sqrt(rng.random()) * spec.agent_spawn_radius
        angle = rng.uniform(0.0, math.tau)
        candidate = (
            _round_coordinate(center_x + math.cos(angle) * radius),
            _round_coordinate(center_y),
            _round_coordinate(center_z + math.sin(angle) * radius),
        )
        if candidate in positions:
            continue
        positions.append(candidate)
        if len(positions) == count:
            return positions
    raise MonsterClearanceScheduleError(
        f"{spec.max_placement_attempts} 次尝试后仍无法找到足够的 Agent 出生点"
    )


def _random_iron_armor(seed: int) -> list[str]:
    """每个 Agent 随机获得 2 件铁防具（不重复部位），由 seed 决定。"""
    rng = random.Random(seed)
    return list(rng.sample(IRON_ARMOR_PIECES, 2))


def _supply_weight_entries(
    generation_spec: MonsterClearanceGenerationSpec,
) -> list[dict[str, int | float | str]]:
    amount_map = dict(generation_spec.supply_amounts)
    return [
        {
            "resource": resource,
            "amount": amount_map.get(resource, 1),
            "weight": weight,
        }
        for resource, weight in generation_spec.supply_weights
    ]


def _command_options(task_data: dict[str, Any]) -> dict[str, str]:
    params = task_data["params"]
    return {
        "monster_tag": str(params["monster_tag"]),
        "supply_tag": str(params["supply_tag"]),
        "spawn_objective": str(params["spawn_objective"]),
    }


def _round_coordinate(value: float) -> float:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return float(int(round(rounded)))
    return rounded
