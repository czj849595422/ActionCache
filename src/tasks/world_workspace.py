"""任务世界存档的一次性部署与丢弃。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from .spec import TaskSpec


class TaskWorldError(RuntimeError):
    """任务世界无法安全准备或清理。"""


def resolve_task_world(spec: TaskSpec, benchmark_root: str | Path) -> Path:
    """从 TaskSpec.environment.world 解析只读基准存档。"""
    world = spec.environment.get("world")
    if not isinstance(world, str) or not world.strip():
        raise TaskWorldError("environment.world 必须指定基准存档名称")
    root = Path(benchmark_root).resolve()
    source = (root / world).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise TaskWorldError("environment.world 不能超出基准存档目录") from exc
    if not source.is_dir():
        raise TaskWorldError(f"基准存档目录不存在: {source}")
    if not (source / "level.dat").is_file():
        raise TaskWorldError(f"基准存档缺少 level.dat: {source}")
    return source


class TaskWorldWorkspace:
    """把只读基准存档部署为一次性活动世界。

    基准存档先复制到 staging，复制完成后直接覆盖服务器活动世界。
    cleanup() 丢弃本次运行副本，任务期间产生的变化不会写回基准存档。
    """

    def __init__(
        self,
        *,
        source_world: str | Path,
        server_dir: str | Path,
    ) -> None:
        self.source_world = Path(source_world).resolve()
        self.server_dir = Path(server_dir).resolve()
        self.level_name = _read_level_name(self.server_dir)
        self.active_world = self.server_dir / self.level_name
        self.staging_world = self.server_dir / f".actioncache-{self.level_name}-staging"
        self.legacy_backup_world = (
            self.server_dir / f".actioncache-{self.level_name}-backup"
        )
        self._validate_paths()
        self._prepared = False

    @property
    def prepared(self) -> bool:
        return self._prepared

    def check_disposable(self) -> None:
        """只读检查一次性世界目标是否可安全部署。

        与 prepare() 相同的路径与锁校验，但不复制、删除或修改任何目录，
        供 preflight 等预检流程在运行前确认目标世界可被安全接管。
        """
        self._validate_paths()
        for label, path in (
            ("活动世界", self.active_world),
            ("staging 世界", self.staging_world),
            ("遗留世界备份", self.legacy_backup_world),
        ):
            if path.exists():
                self._assert_world_removable(path, label)

    def prepare(self) -> None:
        if self._prepared:
            raise TaskWorldError("任务世界已经准备")
        self._validate_source()
        self._remove_world(self.staging_world, "staging 世界")
        try:
            shutil.copytree(self.source_world, self.staging_world)
        except BaseException:
            self._remove_world(self.staging_world, "staging 世界")
            raise

        try:
            self._assert_world_removable(self.active_world, "活动世界")
            self._assert_world_removable(
                self.legacy_backup_world,
                "遗留世界备份",
            )
            self._remove_world(self.legacy_backup_world, "遗留世界备份")
            self._remove_world(self.active_world, "活动世界")
            self.staging_world.replace(self.active_world)
        except BaseException:
            if self.active_world.exists():
                self._remove_world(self.staging_world, "staging 世界")
            elif self.staging_world.exists():
                # 原活动世界已经删除时，尽量保留一份完整的基准世界。
                try:
                    self.staging_world.replace(self.active_world)
                except OSError:
                    pass
            raise
        self._prepared = True

    def cleanup(self) -> None:
        if not self._prepared:
            return
        self._remove_world(self.active_world, "活动世界")
        self._prepared = False

    def _validate_source(self) -> None:
        if not self.source_world.is_dir():
            raise TaskWorldError(f"基准存档目录不存在: {self.source_world}")
        if not (self.source_world / "level.dat").is_file():
            raise TaskWorldError(f"基准存档缺少 level.dat: {self.source_world}")

    def _validate_paths(self) -> None:
        if not self.server_dir.is_dir():
            raise TaskWorldError(f"服务器目录不存在: {self.server_dir}")
        for label, path in (
            ("活动世界", self.active_world),
            ("staging 世界", self.staging_world),
            ("遗留世界备份", self.legacy_backup_world),
        ):
            if path.parent.resolve() != self.server_dir:
                raise TaskWorldError(f"{label}必须是服务器目录的直接子目录: {path}")
        if self.active_world.exists() and self.active_world.resolve().parent != self.server_dir:
            raise TaskWorldError(f"活动世界不能指向服务器目录之外: {self.active_world}")
        disposable_paths = (
            self.active_world,
            self.staging_world,
            self.legacy_backup_world,
        )
        if any(self.source_world == path.resolve() for path in disposable_paths):
            raise TaskWorldError("基准存档不能与服务器任务世界路径相同")

    def _assert_world_removable(self, path: Path, label: str) -> None:
        if not path.exists():
            return
        if not path.is_dir() or path.is_symlink():
            raise TaskWorldError(f"{label}不是普通目录: {path}")
        if path.resolve().parent != self.server_dir:
            raise TaskWorldError(f"拒绝删除服务器目录之外的路径: {path}")
        _assert_world_unlocked(path)

    def _remove_world(self, path: Path, label: str) -> None:
        self._assert_world_removable(path, label)
        if path.exists():
            shutil.rmtree(path)


def _read_level_name(server_dir: Path) -> str:
    properties = server_dir / "server.properties"
    if not properties.is_file():
        raise TaskWorldError(f"服务器目录缺少 server.properties: {properties}")
    values: dict[str, Any] = {}
    for raw_line in properties.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    level_name = str(values.get("level-name", "world"))
    level_path = Path(level_name)
    if (
        not level_name
        or level_path.is_absolute()
        or len(level_path.parts) != 1
        or level_name in {".", ".."}
    ):
        raise TaskWorldError(f"server.properties 的 level-name 不安全: {level_name!r}")
    return level_name


def _assert_world_unlocked(world: Path) -> None:
    """检查 Minecraft session.lock，防止删除仍被服务器使用的世界。"""
    lock_path = world / "session.lock"
    if not lock_path.is_file() or lock_path.stat().st_size == 0:
        return
    try:
        with lock_path.open("r+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
                fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1)
    except OSError as exc:
        raise TaskWorldError(
            f"世界仍被服务器占用，拒绝覆盖或删除: {world}"
        ) from exc
