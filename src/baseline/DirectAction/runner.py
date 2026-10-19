"""DirectAction runner：复用 BasicAsync runner，切换为 direct_action 规划协议。"""

from __future__ import annotations

from typing import Any

from ..BasicAsync import BasicAsyncRunner
from .prompts import DirectActionPromptBuilder


class DirectActionRunner(BasicAsyncRunner):
    """BasicAsync 的无推理直出动作变体（direct_action 协议）。"""

    prompt_builder_class = DirectActionPromptBuilder

    def __init__(self, **kwargs: Any) -> None:
        planning_kwargs = dict(kwargs.pop("planning_worker_kwargs", None) or {})
        planning_kwargs["operation"] = "direct_action"
        planning_kwargs["path_label"] = "direct"
        acting_kwargs = dict(kwargs.pop("acting_worker_kwargs", None) or {})
        acting_kwargs["plan_source"] = "direct"
        super().__init__(
            planning_worker_kwargs=planning_kwargs,
            acting_worker_kwargs=acting_kwargs,
            **kwargs,
        )


__all__ = ["DirectActionRunner"]
