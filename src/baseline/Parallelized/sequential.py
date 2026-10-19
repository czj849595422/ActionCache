"""PPA 顺序多 episode 运行器（薄包装泛型 SequentialBaselineRunner）。"""

from __future__ import annotations

from ..sequential import SequentialBaselineRunner
from .runner import PPARunner


class PPASequentialRunner(SequentialBaselineRunner):
    def __init__(self, **kwargs) -> None:
        super().__init__(runner_class=PPARunner, **kwargs)


__all__ = ["PPASequentialRunner"]
