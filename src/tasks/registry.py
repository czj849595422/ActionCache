"""任务注册表：按 task_id 注册/加载任务规格，按 category 映射管理器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .manager import TaskManager
from .spec import TaskSpec, TaskSpecError, load_spec


class TaskRegistryError(ValueError):
    """任务注册表错误。"""


class TaskRegistry:
    """任务规格与类别管理器的统一注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, TaskSpec] = {}
        self._managers: dict[str, Callable[..., TaskManager]] = {}

    def load(self, path: str | Path) -> TaskSpec:
        """从 YAML 文件加载并注册一个任务规格。"""
        spec = load_spec(path)
        self.register_spec(spec)
        return spec

    def register_spec(self, spec: TaskSpec) -> None:
        if spec.task_id in self._specs:
            raise TaskRegistryError(f"重复的 task_id: {spec.task_id}")
        self._specs[spec.task_id] = spec

    def get(self, task_id: str) -> TaskSpec:
        try:
            return self._specs[task_id]
        except KeyError as exc:
            raise TaskRegistryError(f"未知 task_id: {task_id}") from exc

    def list(self, category: str | None = None) -> list[TaskSpec]:
        specs = [
            spec for spec in self._specs.values()
            if category is None or spec.category == category
        ]
        return sorted(specs, key=lambda spec: spec.task_id)

    def register_manager(
        self,
        category: str,
        factory: Callable[..., TaskManager],
        *,
        overwrite: bool = False,
    ) -> None:
        if category in self._managers and not overwrite:
            raise TaskRegistryError(f"category 管理器已注册: {category}")
        if not callable(factory):
            raise TaskRegistryError("factory 必须是可调用对象")
        self._managers[category] = factory

    def manager_for(self, category: str) -> Callable[..., TaskManager]:
        try:
            return self._managers[category]
        except KeyError as exc:
            raise TaskRegistryError(f"未注册 category 管理器: {category}") from exc

    def categories(self) -> list[str]:
        return sorted(self._managers)

    def to_summary(self) -> dict[str, Any]:
        return {
            "tasks": [spec.task_id for spec in self.list()],
            "categories": self.categories(),
        }
