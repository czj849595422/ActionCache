"""Sequential multi-episode runner for the AutoGen baseline."""

from __future__ import annotations

from ..sequential import SequentialBaselineRunner
from .runner import AutoGenRunner


class AutoGenSequentialRunner(SequentialBaselineRunner):
    def __init__(self, **kwargs) -> None:
        super().__init__(runner_class=AutoGenRunner, **kwargs)


__all__ = ["AutoGenSequentialRunner"]
