"""DirectAction：无推理直出动作 baseline 的配置契约。"""

from __future__ import annotations

from ..BasicAsync import BasicAsyncConfig


class DirectActionConfig(BasicAsyncConfig):
    """DirectAction 运行时配置：复用 BasicAsync 全部配置，仅作方法身份标识。"""


__all__ = ["DirectActionConfig"]
