"""Seed-based task preset expansion.

This keeps the existing TaskSpec/manager path intact while allowing selected
tasks to be launched from one shared preset file plus a random seed.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml

from .spec import TaskSpec, TaskSpecError


TASKS_DIR = Path(__file__).resolve().parent
DEFAULT_SEED_PRESETS_FILE = TASKS_DIR / "seed_presets.yaml"
_SEEDED_TASK_ARG_RE = re.compile(r"^(?P<task>[A-Za-z0-9_.-]+)@(?P<seed>-?\d+)$")


class TaskSeedPresetError(ValueError):
    """Seed preset loading or expansion failed."""


def parse_seeded_task_arg(raw: str) -> tuple[str, int] | None:
    """Parse ``task_id@seed`` task arguments."""
    match = _SEEDED_TASK_ARG_RE.fullmatch(str(raw).strip())
    if match is None:
        return None
    return match.group("task"), int(match.group("seed"))


def list_seed_preset_ids(
    presets_file: str | Path = DEFAULT_SEED_PRESETS_FILE,
) -> tuple[str, ...]:
    """Return task ids that can be expanded from seed presets."""
    return tuple(sorted(_load_seed_presets(presets_file)))


def seeded_task_spec(
    task_id: str,
    seed: int,
    *,
    presets_file: str | Path = DEFAULT_SEED_PRESETS_FILE,
) -> TaskSpec:
    """Expand one seedable task preset into a complete ``TaskSpec``."""
    if not isinstance(seed, int):
        raise TaskSeedPresetError("seed 必须是整数")
    presets = _load_seed_presets(presets_file)
    try:
        data = deepcopy(presets[task_id])
    except KeyError as exc:
        available = ", ".join(sorted(presets)) or "<none>"
        raise TaskSeedPresetError(
            f"任务 {task_id!r} 不支持 seed preset；可用: {available}"
        ) from exc

    params = dict(data.get("params") or {})
    params["generation_seed"] = seed
    data["params"] = params

    environment = dict(data.get("environment") or {})
    environment["seed"] = seed
    data["environment"] = environment

    metadata = dict(data.get("metadata") or {})
    metadata.update(
        {
            "generation_seed": seed,
            "seed_preset": True,
            "seed_preset_file": Path(presets_file).name,
        }
    )
    data["metadata"] = metadata

    if data.get("category") == "pvp":
        from .pvp.spawns import PVPSpawnError, pvp_task_data_with_seeded_spawns

        try:
            data = pvp_task_data_with_seeded_spawns(data, seed=seed)
        except PVPSpawnError as exc:
            raise TaskSeedPresetError(
                f"seed preset {task_id!r} 出生点生成失败: {exc}"
            ) from exc

    base_name = str(data.get("name") or task_id)
    if "(seed " not in base_name:
        data["name"] = f"{base_name} (seed {seed})"

    try:
        spec = TaskSpec.from_dict(data)
    except TaskSpecError as exc:
        raise TaskSeedPresetError(f"seed preset {task_id!r} 展开后非法: {exc}") from exc

    # A virtual config path keeps task-folder descriptions working without
    # requiring one YAML file per seed.
    spec._source_path = (TASKS_DIR / task_id / "config" / f"seed-{seed}.yaml").resolve()
    return spec


def _load_seed_presets(path: str | Path) -> dict[str, dict[str, Any]]:
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TaskSeedPresetError(f"无法读取 seed preset 文件 {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaskSeedPresetError(f"seed preset 文件 {target} 顶层必须是映射")
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise TaskSeedPresetError(f"seed preset 文件 {target} 缺少 tasks 映射")
    result: dict[str, dict[str, Any]] = {}
    for task_id, data in tasks.items():
        if not isinstance(task_id, str) or not task_id:
            raise TaskSeedPresetError("seed preset 的 task_id 必须是非空字符串")
        if not isinstance(data, dict):
            raise TaskSeedPresetError(f"seed preset {task_id!r} 必须是映射")
        result[task_id] = data
    return result


__all__ = [
    "DEFAULT_SEED_PRESETS_FILE",
    "TaskSeedPresetError",
    "list_seed_preset_ids",
    "parse_seeded_task_arg",
    "seeded_task_spec",
]
