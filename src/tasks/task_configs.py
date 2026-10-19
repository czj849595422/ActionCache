"""任务初始化文件与 runtime seed 任务展开。"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .spec import TaskSpec, TaskSpecError, load_spec
from .seed_presets import (
    TaskSeedPresetError,
    parse_seeded_task_arg,
    seeded_task_spec,
)
from .runtime_generation import (
    RUNTIME_SEEDED_TASK_IDS,
    runtime_seeded_task_spec,
)


TASKS_DIR = Path(__file__).resolve().parent
SPECS_DIR = TASKS_DIR
YAML_SUFFIXES = {".yaml", ".yml"}


class TaskConfigError(ValueError):
    """任务 config 目录或初始化文件解析错误。"""


def task_config_dir(
    task_name: str,
    tasks_dir: str | Path = TASKS_DIR,
) -> Path:
    """任务名 → 任务文件夹。

    ``task_name`` 可以是已存在的目录路径（直接使用），也可以是内置 task_id
    （解析为 ``tasks_dir/<task_id>``）。返回的任务文件夹下应有 ``config/``。
    """
    path = Path(task_name)
    if path.is_dir():
        return path.resolve()
    folder = (Path(tasks_dir) / task_name).resolve()
    if not folder.is_dir():
        raise TaskConfigError(f"任务文件夹不存在: {folder}")
    return folder


def list_task_configs(task_folder: str | Path) -> list[Path]:
    """扫描任务文件夹 ``config/*.yaml``，按编号（文件名数字）升序返回。"""
    config_dir = Path(task_folder) / "config"
    if not config_dir.is_dir():
        raise TaskConfigError(f"任务缺少 config 目录: {config_dir}")
    files = [
        path
        for path in config_dir.glob("*.yaml")
        if path.is_file() and path.suffix.lower() in YAML_SUFFIXES
    ]
    files.sort(key=_config_sort_key)
    if not files:
        raise TaskConfigError(f"config 目录为空: {config_dir}")
    return files


def _config_sort_key(path: Path):
    stem = path.stem
    try:
        return (0, int(stem), stem)
    except ValueError:
        return (1, 0, stem)


def expand_task_args(
    task_args: Sequence[str],
    tasks_dir: str | Path = TASKS_DIR,
    *,
    seed: int | None = None,
    start_episode: int = 1,
    episode_count: int | None = None,
) -> list[TaskSpec]:
    """把 demo 的 ``--task`` 参数展开为 TaskSpec 列表。

    - 值指向 ``.yaml``/``.yml`` 文件时按单文件原样加载（显式配置）；
    - 这五个任务 ``boss_combat`` / ``mining`` / ``monster_clearance`` / ``pvp`` / ``trade``
      会直接按 seed 生成 runtime spec，默认 seed 为 ``0``；
    - 值形如 ``task_id@seed`` 时从统一 seed preset 或 runtime 生成展开；
    - 传入 ``seed`` 时，支持 seed preset 或 runtime 任务的任务名从对应生成器展开；
    - 其他任务名仍按该任务 ``config/`` 下的全部编号初始化文件展开。
    """
    if start_episode < 1:
        raise TaskConfigError("start_episode 必须 >= 1")
    if episode_count is not None and episode_count < 1:
        raise TaskConfigError("episode_count 必须 >= 1")
    if seed is not None and len(task_args) != 1:
        raise TaskConfigError("--task-seed 仅支持单个 --task")
    specs: list[TaskSpec] = []
    single_task_arg = len(task_args) == 1
    for raw in task_args:
        seeded = parse_seeded_task_arg(raw)
        if seeded is not None:
            if seed is not None:
                raise TaskConfigError("不能同时使用 task@seed 与 --task-seed")
            task_id, task_seed = seeded
            try:
                if task_id in RUNTIME_SEEDED_TASK_IDS:
                    builder = runtime_seeded_task_spec
                else:
                    builder = seeded_task_spec
                if single_task_arg:
                    specs.extend(
                        _seeded_specs_for_episode_window(
                            task_id,
                            task_seed,
                            start_episode=start_episode,
                            episode_count=episode_count,
                            builder=builder,
                        )
                    )
                else:
                    specs.append(builder(task_id, task_seed))
            except TaskSeedPresetError as exc:
                raise TaskConfigError(str(exc)) from exc
            except TaskSpecError as exc:
                raise TaskConfigError(str(exc)) from exc
            continue
        path = Path(raw)
        if path.suffix.lower() in YAML_SUFFIXES and path.is_file():
            specs.append(load_spec(path))
            continue
        if raw in RUNTIME_SEEDED_TASK_IDS:
            try:
                if single_task_arg:
                    specs.extend(
                        _seeded_specs_for_episode_window(
                            raw,
                            seed or 0,
                            start_episode=start_episode,
                            episode_count=episode_count,
                            builder=runtime_seeded_task_spec,
                        )
                    )
                else:
                    specs.append(runtime_seeded_task_spec(raw, seed or 0))
            except TaskSpecError as exc:
                raise TaskConfigError(str(exc)) from exc
            continue
        if seed is not None:
            try:
                specs.extend(
                    _seeded_specs_for_episode_window(
                        raw,
                        seed,
                        start_episode=start_episode,
                        episode_count=episode_count,
                        builder=seeded_task_spec,
                    )
                )
            except TaskSeedPresetError as exc:
                raise TaskConfigError(str(exc)) from exc
            except TaskSpecError as exc:
                raise TaskConfigError(str(exc)) from exc
            continue
        for config_path in list_task_configs(task_config_dir(raw, tasks_dir)):
            specs.append(load_spec(config_path))
    if not specs:
        raise TaskConfigError("未解析到任何任务初始化文件")
    return specs


def _seeded_specs_for_episode_window(
    task_id: str,
    first_episode_seed: int,
    *,
    start_episode: int,
    episode_count: int | None,
    builder,
) -> list[TaskSpec]:
    count = episode_count or 1
    generated = start_episode + count - 1
    return [
        builder(task_id, first_episode_seed + index)
        for index in range(generated)
    ]


__all__ = [
    "TASKS_DIR",
    "SPECS_DIR",
    "TaskConfigError",
    "expand_task_args",
    "list_task_configs",
    "task_config_dir",
]
