"""COAP 顺序多 episode 运行器（薄包装泛型 SequentialBaselineRunner）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..sequential import (
    SequentialBaselineEpisodeResult as SequentialEpisodeResult,
    SequentialBaselineResult as SequentialCOAPResult,
    SequentialBaselineRunner,
)
from .config import COAPConfig, CacheBootMode, CacheSharingMode
from .runner import COAPRunner, load_shared_cache_snapshot


class COAPSequentialRunner(SequentialBaselineRunner):
    def __init__(self, **kwargs) -> None:
        user_runner_kwargs_factory = kwargs.pop("runner_kwargs_factory", None)
        kwargs.setdefault("episode_retries", 1)
        if kwargs.get("config") is None:
            kwargs["config"] = COAPConfig()
        config = kwargs["config"]
        run_recorder = kwargs.get("run_recorder")

        def runner_kwargs_factory(
            spec: Any,
            manager: Any,
            episode: Any,
            episode_number: int,
        ) -> dict[str, Any]:
            values: dict[str, Any] = {}
            if user_runner_kwargs_factory is not None:
                values = user_runner_kwargs_factory(
                    spec, manager, episode, episode_number
                )
                if not isinstance(values, dict):
                    raise ValueError("runner_kwargs_factory 必须返回 dict")
            if config.cache_boot.resolved_mode is not CacheBootMode.REUSE_PREVIOUS:
                return values
            if config.cache_sharing is not CacheSharingMode.SHARED:
                raise ValueError("reuse_previous 当前仅支持 shared cache")
            if episode_number == 1:
                return values
            if run_recorder is None:
                raise ValueError("reuse_previous 顺序运行需要 run_recorder")
            previous = (
                Path(run_recorder.path)
                / str(episode_number - 1)
                / "cache"
                / "shared.json"
            )
            if not previous.is_file():
                raise FileNotFoundError(
                    f"reuse_previous 找不到上一 episode 缓存: {previous}"
                )
            values["cache_source"] = load_shared_cache_snapshot(previous)
            return values

        super().__init__(
            runner_class=COAPRunner,
            runner_kwargs_factory=runner_kwargs_factory,
            **kwargs,
        )


__all__ = [
    "COAPSequentialRunner",
    "SequentialCOAPResult",
    "SequentialEpisodeResult",
]
