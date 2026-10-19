"""从 YAML 模板批量生成动态矿物任务包。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.tasks.spec import TaskSpec, TaskSpecError

from .schedule import (
    MiningBounds,
    MiningGenerationSpec,
    MiningScheduleError,
    OreChoice,
    OreSpawnRecord,
    generate_spawn_schedule,
    write_command_file,
    write_spawn_schedule,
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
        raise MiningScheduleError(f"无法解析 mining 模板 {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise MiningScheduleError("mining 模板顶层必须是映射")
    if not isinstance(data.get("task"), dict) or not isinstance(
        data.get("generation"), dict
    ):
        raise MiningScheduleError("mining 模板必须包含 task 和 generation 映射")
    if not data.get("task_id_prefix") or not data.get("name_prefix"):
        raise MiningScheduleError("task_id_prefix 和 name_prefix 必填")
    return data


def generation_spec_from_template(template: dict[str, Any]) -> MiningGenerationSpec:
    generation = template["generation"]
    task = template["task"]
    completion = task.get("completion") or {}
    success = completion.get("success") or {}
    duration = generation.get("duration_seconds") or {}
    ores_raw = generation.get("ores") or []
    if not isinstance(ores_raw, list):
        raise MiningScheduleError("generation.ores 必须是列表")
    ores: list[OreChoice] = []
    for index, item in enumerate(ores_raw):
        if not isinstance(item, dict):
            raise MiningScheduleError(f"generation.ores[{index}] 必须是映射")
        try:
            ores.append(
                OreChoice(
                    block=str(item["block"]),
                    item=str(item.get("item") or str(item["block"]).split(":")[-1]),
                    value=float(item["value"]),
                    weight=float(item["weight"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MiningScheduleError(
                f"generation.ores[{index}] 字段非法: {exc}"
            ) from exc
    exclusions = generation.get("exclusion_centers") or []
    try:
        exclusion_centers = tuple(
            (int(item["x"]), int(item["z"])) for item in exclusions
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MiningScheduleError(f"exclusion_centers 非法: {exc}") from exc
    try:
        return MiningGenerationSpec(
            bounds=MiningBounds.from_dict(generation["bounds"]),
            timeout_seconds=int(completion["timeout_seconds"]),
            target_value=float(success["target_value"]),
            wave_interval_seconds=int(generation["wave_interval_seconds"]),
            groups_per_wave=int(generation["groups_per_wave"]),
            duration_min_seconds=int(duration["min"]),
            duration_max_seconds=int(duration["max"]),
            group_sizes=tuple(int(size) for size in generation.get("group_sizes", [3, 4])),
            min_anchor_distance=float(generation["min_anchor_distance"]),
            ores=tuple(ores),
            exclusion_centers=exclusion_centers,
            exclusion_radius=float(generation.get("exclusion_radius", 0)),
            max_placement_attempts=int(generation.get("max_placement_attempts", 5000)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MiningScheduleError(f"mining 生成参数非法: {exc}") from exc


def generate_task_batch(
    template_path: str | Path,
    *,
    count: int,
    base_seed: int,
    output_dir: str | Path,
    overwrite: bool = False,
) -> list[TaskBundlePaths]:
    """生成多个任务目录；每个目录包含 YAML、时间表和命令文件。"""
    if count < 1:
        raise ValueError("count 必须大于 0")
    template = load_generation_template(template_path)
    generation_spec = generation_spec_from_template(template)
    root = Path(output_dir)
    task_ids = [f"{template['task_id_prefix']}.{base_seed + index}" for index in range(count)]
    directories = [root / task_id for task_id in task_ids]
    if not overwrite:
        occupied = [path for path in directories if path.exists()]
        if occupied:
            raise FileExistsError(f"输出目录已存在: {occupied[0]}")

    prepared: list[tuple[Path, list[OreSpawnRecord], dict[str, Any]]] = []
    for index, (task_id, directory) in enumerate(zip(task_ids, directories)):
        seed = base_seed + index
        records = generate_spawn_schedule(generation_spec, seed)
        task_data = build_task_data(
            template,
            generation_spec,
            task_id=task_id,
            seed=seed,
        )
        try:
            TaskSpec.from_dict(task_data)
        except TaskSpecError as exc:
            raise MiningScheduleError(f"生成任务规格非法: {exc}") from exc
        prepared.append((directory, records, task_data))

    bundles: list[TaskBundlePaths] = []
    for directory, records, task_data in prepared:
        directory.mkdir(parents=True, exist_ok=overwrite)
        schedule_path = directory / "spawn_schedule.txt"
        commands_path = directory / "commands.txt"
        task_path = directory / "task.yaml"
        write_spawn_schedule(records, schedule_path)
        write_command_file(records, commands_path)
        task_path.write_text(
            yaml.safe_dump(task_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        bundles.append(
            TaskBundlePaths(
                directory=directory,
                task=task_path,
                schedule=schedule_path,
                commands=commands_path,
            )
        )
    return bundles


def build_task_data(
    template: dict[str, Any],
    generation_spec: MiningGenerationSpec,
    *,
    task_id: str,
    seed: int,
) -> dict[str, Any]:
    task = deepcopy(template["task"])
    task.pop("description", None)
    task["task_id"] = task_id
    task["name"] = f"{template['name_prefix']} (seed {seed})"
    success = task["completion"]["success"]
    success["item_values"] = {
        ore.item: _number(ore.value) for ore in generation_spec.ores
    }
    params = dict(task.get("params") or {})
    params.update(
        {
            "schedule_file": "spawn_schedule.txt",
            "command_file": "commands.txt",
            "bounds": generation_spec.bounds.to_dict(),
            "min_anchor_distance": generation_spec.min_anchor_distance,
            "ore_blocks": {ore.block: ore.item for ore in generation_spec.ores},
            "generation_seed": seed,
        }
    )
    task["params"] = params
    metadata = dict(task.get("metadata") or {})
    metadata["generation_seed"] = seed
    task["metadata"] = metadata
    return task


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
