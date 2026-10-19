"""AgenticCache sequential episode runner."""

from __future__ import annotations

from ..sequential import SequentialBaselineRunner
from .runner import AgenticCacheRunner


class AgenticCacheSequentialRunner(SequentialBaselineRunner):
    def __init__(self, **kwargs) -> None:
        super().__init__(runner_class=AgenticCacheRunner, **kwargs)


__all__ = ["AgenticCacheSequentialRunner"]
