"""从 YAML 模板批量生成多阶段合成贸易任务包。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.tasks.spec import TaskSpec, TaskSpecError

from .config import (
    BASE_PRICES,
    DEFAULT_PROFESSIONS,
    TARGET_POOL,
    TradeConfigError,
    TradeGenerationSpec,
    TradeProfessionConfig,
    TradeTaskConfig,
    generate_trade_config,
    write_command_file,
    write_trade_config,
)


@dataclass(frozen=True)
class TaskBundlePaths:
    directory: Path
    task: Path
    trade_config: Path
    commands: Path


def load_generation_template(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TradeConfigError(f"无法解析 trade 模板 {target}: {exc}") from exc
    if not isinstance(value, dict):
        raise TradeConfigError("trade 模板顶层必须是映射")
    if not isinstance(value.get("task"), dict) or not isinstance(
        value.get("generation"), dict
    ):
        raise TradeConfigError("trade 模板必须包含 task 和 generation 映射")
    if not value.get("task_id_prefix") or not value.get("name_prefix"):
        raise TradeConfigError("task_id_prefix 和 name_prefix 必填")
    return value


def generation_spec_from_template(template: dict[str, Any]) -> TradeGenerationSpec:
    generation = template["generation"]
    material_counts = generation.get("material_count") or {}
    raw_prices = generation.get("base_prices") or BASE_PRICES
    raw_targets = generation.get("target_pool") or TARGET_POOL
    raw_professions = generation.get("professions")
    if not isinstance(raw_prices, dict) or not isinstance(raw_targets, list):
        raise TradeConfigError("generation.base_prices/target_pool 类型非法")
    if raw_professions is None:
        professions = DEFAULT_PROFESSIONS
    elif isinstance(raw_professions, dict):
        professions = tuple(
            (
                str(profession_id),
                TradeProfessionConfig.from_dict(definition, str(profession_id)),
            )
            for profession_id, definition in raw_professions.items()
        )
    else:
        raise TradeConfigError("generation.professions 必须是映射")
    try:
        spec = TradeGenerationSpec(
            base_prices=tuple((str(item), int(price)) for item, price in raw_prices.items()),
            target_pool=tuple(str(item) for item in raw_targets),
            professions=professions,
            supply_margin=float(generation.get("supply_margin", 0.25)),
            value_multiplier=float(generation.get("value_multiplier", 1.5)),
            material_count_min=int(material_counts.get("min", 16)),
            material_count_max=int(material_counts.get("max", 48)),
            max_attempts=int(generation.get("max_attempts", 1000)),
        )
    except (TypeError, ValueError) as exc:
        raise TradeConfigError(f"trade 生成参数非法: {exc}") from exc
    if spec.material_count_min < 1 or spec.material_count_max < spec.material_count_min:
        raise TradeConfigError("材料数量范围非法")
    expected_pool = tuple(
        dict.fromkeys(
            item
            for _, profession in spec.professions
            for item in (*profession.tool_targets, *profession.food_targets)
        )
    )
    if tuple(spec.target_pool) != expected_pool:
        raise TradeConfigError("target_pool 必须是四种职业候选目标的并集")
    if spec.max_attempts < 1:
        raise TradeConfigError("max_attempts 必须大于 0")
    return spec


def generate_task_batch(
    template_path: str | Path,
    *,
    count: int,
    base_seed: int,
    output_dir: str | Path,
    overwrite: bool = False,
) -> list[TaskBundlePaths]:
    """原子生成多个任务目录，每个目录包含任务、贸易配置与指令。"""
    if count < 1:
        raise ValueError("count 必须大于 0")
    template = load_generation_template(template_path)
    generation_spec = generation_spec_from_template(template)
    root = Path(output_dir)
    task_ids = [
        f"{template['task_id_prefix']}.{base_seed + index}" for index in range(count)
    ]
    directories = [root / task_id for task_id in task_ids]
    if not overwrite:
        occupied = [directory for directory in directories if directory.exists()]
        if occupied:
            raise FileExistsError(f"输出目录已存在: {occupied[0]}")

    prepared: list[tuple[Path, dict[str, Any], TradeTaskConfig]] = []
    for index, (task_id, directory) in enumerate(zip(task_ids, directories)):
        seed = base_seed + index
        config = generate_trade_config(generation_spec, seed)
        task_data = build_task_data(template, task_id=task_id, seed=seed)
        try:
            TaskSpec.from_dict(task_data)
        except TaskSpecError as exc:
            raise TradeConfigError(f"生成任务规格非法: {exc}") from exc
        prepared.append((directory, task_data, config))

    bundles: list[TaskBundlePaths] = []
    for directory, task_data, config in prepared:
        directory.mkdir(parents=True, exist_ok=overwrite)
        task_path = directory / "task.yaml"
        config_path = directory / "trade_config.yaml"
        commands_path = directory / "commands.txt"
        write_trade_config(config, config_path)
        write_command_file(config, commands_path)
        task_path.write_text(
            yaml.safe_dump(task_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        bundles.append(
            TaskBundlePaths(
                directory=directory,
                task=task_path,
                trade_config=config_path,
                commands=commands_path,
            )
        )
    return bundles


def build_task_data(
    template: dict[str, Any],
    *,
    task_id: str,
    seed: int,
    config_file: str = "trade_config.yaml",
    command_file: str = "commands.txt",
) -> dict[str, Any]:
    task = deepcopy(template["task"])
    task.pop("description", None)
    task["task_id"] = task_id
    task["name"] = f"{template['name_prefix']} (seed {seed})"
    params = dict(task.get("params") or {})
    params.update(
        {
            "trade_config_file": config_file,
            "command_file": command_file,
            "generation_seed": seed,
        }
    )
    task["params"] = params
    metadata = dict(task.get("metadata") or {})
    metadata["generation_seed"] = seed
    task["metadata"] = metadata
    return task
