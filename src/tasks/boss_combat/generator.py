"""Generate Boss combat initialization configs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import random
from typing import Any

import yaml

from src.tasks.spec import TaskSpec, TaskSpecError

from .arena import is_boss_spawn_inside


DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "specs" / "boss_combat.yaml"


@dataclass(frozen=True)
class BossCombatGenerationSpec:
    agent_spawn: tuple[float, float, float]
    min_distance: float = 41.0
    max_distance: float = 50.0
    y_offset: float = 1.0
    agent_count: int = 4
    agent_spawn_radius: float = 20.0


@dataclass(frozen=True)
class TaskBundlePaths:
    directory: Path
    task: Path


def load_task_template(path: str | Path = DEFAULT_TEMPLATE) -> dict[str, Any]:
    target = Path(path)
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"无法解析 boss_combat 模板 {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("boss_combat 模板顶层必须是映射")
    return data


def generation_spec_from_task(task_data: dict[str, Any]) -> BossCombatGenerationSpec:
    try:
        position = task_data["initial_state"]["per_agent"]["position"]
        params = task_data.get("params") or {}
        return BossCombatGenerationSpec(
            agent_spawn=(
                float(position["x"]),
                float(position["y"]),
                float(position["z"]),
            ),
            min_distance=float(params.get("boss_spawn_min_distance", 41.0)),
            max_distance=float(params.get("boss_spawn_max_distance", 50.0)),
            y_offset=float(params.get("boss_spawn_y_offset", 1.0)),
            agent_count=int((task_data.get("agents") or {}).get("count", 4)),
            agent_spawn_radius=float(params.get("agent_spawn_radius", 20.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"boss_combat 生成参数非法: {exc}") from exc


def generate_boss_spawn_position(
    seed: int,
    spec: BossCombatGenerationSpec,
) -> dict[str, float]:
    if spec.min_distance <= 40 or spec.max_distance < spec.min_distance:
        raise ValueError("Boss 生成距离必须满足 40 < min_distance <= max_distance")
    rng = random.Random(seed)
    x, y, z = spec.agent_spawn
    for _ in range(5000):
        distance = rng.uniform(spec.min_distance, spec.max_distance)
        angle = rng.uniform(0.0, math.tau)
        position = {
            "x": _round_coordinate(x + math.cos(angle) * distance),
            "y": _round_coordinate(y + spec.y_offset),
            "z": _round_coordinate(z + math.sin(angle) * distance),
        }
        if is_boss_spawn_inside(position):
            return position
    raise ValueError("无法在黑曜石围墙范围内生成满足距离约束的 Boss 出生点")


def generate_agent_spawn_positions(
    seed: int,
    spec: BossCombatGenerationSpec,
) -> list[dict[str, float]]:
    """Generate distinct Agent positions inside the center radius."""
    if spec.agent_count < 1 or spec.agent_spawn_radius <= 0:
        raise ValueError("Agent 出生点数量和范围必须大于 0")
    rng = random.Random(seed ^ 0xA6E17B9D)
    center_x, center_y, center_z = spec.agent_spawn
    positions: list[dict[str, float]] = []
    for _ in range(5000):
        radius = math.sqrt(rng.random()) * spec.agent_spawn_radius
        angle = rng.uniform(0.0, math.tau)
        candidate = {
            "x": _round_coordinate(center_x + math.cos(angle) * radius),
            "y": _round_coordinate(center_y),
            "z": _round_coordinate(center_z + math.sin(angle) * radius),
        }
        if candidate in positions:
            continue
        positions.append(candidate)
        if len(positions) == spec.agent_count:
            return positions
    raise ValueError("无法生成足够的 Agent 出生点")


def build_task_data(
    template: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    task_data = deepcopy(template)
    task_data.pop("description", None)
    generation_spec = generation_spec_from_task(task_data)
    agent_positions = generate_agent_spawn_positions(seed, generation_spec)
    agent_names = list((task_data.get("agents") or {}).get("names") or [])
    if not agent_names:
        defaults = ("Alice", "Bob", "Carol", "Dave", "Eve")
        agent_names = list(defaults[: generation_spec.agent_count])
        if len(agent_names) < generation_spec.agent_count:
            agent_names.extend(
                f"agent-{index}"
                for index in range(len(agent_names), generation_spec.agent_count)
            )
    if len(agent_names) != len(agent_positions):
        raise ValueError("Boss Agent 名称数量与出生点数量不一致")
    spawns = list(task_data.get("spawns") or [])
    if not spawns:
        raise ValueError("boss_combat 模板必须包含至少一个 spawns 项")
    spawns[0] = dict(spawns[0])
    spawns[0]["position"] = generate_boss_spawn_position(seed, generation_spec)
    task_data["spawns"] = spawns
    initial_state = deepcopy(task_data.get("initial_state") or {})
    per_agent = dict(initial_state.get("per_agent") or {})
    loadout = generate_boss_initial_loadout(seed)
    inventory = dict(per_agent.get("inventory") or {})
    inventory = {
        item: count
        for item, count in inventory.items()
        if _item_name(item) != "diamond_sword"
    }
    inventory[loadout["sword"]] = 1
    per_agent["inventory"] = inventory
    per_agent["equipment"] = list(loadout["equipment"])
    initial_state["per_agent"] = per_agent
    initial_state["by_agent"] = [
        {"name": name, "position": position}
        for name, position in zip(agent_names, agent_positions)
    ]
    task_data["initial_state"] = initial_state
    params = dict(task_data.get("params") or {})
    params["generation_seed"] = seed
    params.setdefault("boss_spawn_min_distance", generation_spec.min_distance)
    params.setdefault("boss_spawn_max_distance", generation_spec.max_distance)
    params.setdefault("boss_spawn_y_offset", generation_spec.y_offset)
    params.setdefault("agent_spawn_radius", generation_spec.agent_spawn_radius)
    task_data["params"] = params
    metadata = dict(task_data.get("metadata") or {})
    metadata["generation_seed"] = seed
    task_data["metadata"] = metadata
    try:
        TaskSpec.from_dict(task_data)
    except TaskSpecError as exc:
        raise ValueError(f"生成 boss_combat 任务规格非法: {exc}") from exc
    return task_data


def generate_task_batch(
    template_path: str | Path = DEFAULT_TEMPLATE,
    *,
    count: int,
    base_seed: int,
    output_dir: str | Path,
    overwrite: bool = False,
) -> list[TaskBundlePaths]:
    if count < 1:
        raise ValueError("count 必须大于 0")
    template = load_task_template(template_path)
    root = Path(output_dir)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    bundles: list[TaskBundlePaths] = []
    for index in range(count):
        seed = base_seed + index
        task_path = config_dir / f"{seed}.yaml"
        if task_path.exists() and not overwrite:
            raise FileExistsError(f"输出文件已存在: {task_path}")
        task_data = build_task_data(template, seed=seed)
        task_path.write_text(
            yaml.safe_dump(task_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        bundles.append(TaskBundlePaths(directory=config_dir, task=task_path))
    return bundles


def _round_coordinate(value: float) -> float:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return float(int(round(rounded)))
    return rounded


def generate_boss_initial_loadout(seed: int) -> dict[str, Any]:
    """Generate a deterministic enchanted Boss combat loadout.

    The generated values keep the template enchantments as minimum levels while
    limiting each item to enchantments that are valid for its equipment type.
    """
    rng = random.Random(seed ^ 0x5EEDB055)

    def optional_unbreaking() -> tuple[str, int] | None:
        if rng.random() < 0.5:
            return ("unbreaking", rng.randint(1, 3))
        return None

    def item(name: str, enchantments: list[tuple[str, int]]) -> str:
        entries = ",".join(
            f'{{id:"minecraft:{enchantment}",lvl:{level}s}}'
            for enchantment, level in enchantments
        )
        return f"{name}{{Enchantments:[{entries}]}}"

    def armor(name: str, *, boots: bool = False) -> str:
        enchantments = [("protection", rng.randint(3, 4))]
        if boots:
            enchantments.append(("feather_falling", rng.randint(3, 4)))
        extra = optional_unbreaking()
        if extra is not None:
            enchantments.append(extra)
        return item(name, enchantments)

    sword_enchantment = (
        "sharpness" if rng.random() < 0.5 else "smite",
        rng.randint(3, 5),
    )
    sword_extras = [sword_enchantment]
    extra = optional_unbreaking()
    if extra is not None:
        sword_extras.append(extra)

    bow_enchantments = [("power", rng.randint(3, 5))]
    if rng.random() < 0.5:
        bow_enchantments.append(("infinity", 1))
    extra = optional_unbreaking()
    if extra is not None:
        bow_enchantments.append(extra)

    return {
        "sword": item("diamond_sword", sword_extras),
        "equipment": [
            item("bow", bow_enchantments),
            armor("diamond_helmet"),
            armor("diamond_chestplate"),
            armor("diamond_leggings"),
            armor("diamond_boots", boots=True),
        ],
    }


def _item_name(value: object) -> str:
    return str(value or "").strip().lower().removeprefix("minecraft:").split("{", 1)[0]
