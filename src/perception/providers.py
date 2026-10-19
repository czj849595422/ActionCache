"""Read-only server-side providers for perception enrichment."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from src.utils.snbt import parse_first_snbt_compound


class BlockEntityQueryError(RuntimeError):
    """A block entity could not be read without mutating the world."""


class BlockEntityProvider(Protocol):
    def read_block_entity(
        self,
        position: Mapping[str, Any],
        block_name: str,
    ) -> dict[str, Any]:
        ...


@dataclass
class _CacheEntry:
    value: dict[str, Any]
    cached_at: float
    observed_at_ms: int


class RconBlockEntityProvider:
    """Read block entity NBT through ``data get block`` with a short cache."""

    def __init__(
        self,
        server: Any,
        *,
        cache_ttl_ms: int = 500,
        stale_if_error_ms: int = 5_000,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not hasattr(server, "send_command"):
            raise TypeError("server must provide send_command(command)")
        if cache_ttl_ms < 0 or stale_if_error_ms < cache_ttl_ms:
            raise ValueError("invalid block entity cache durations")
        self.server = server
        self.cache_ttl_ms = cache_ttl_ms
        self.stale_if_error_ms = stale_if_error_ms
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._cache: dict[tuple[int, int, int, str], _CacheEntry] = {}

    def read_block_entity(
        self,
        position: Mapping[str, Any],
        block_name: str,
    ) -> dict[str, Any]:
        x, y, z = _position_key(position)
        key = (x, y, z, str(block_name))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and _age_ms(now, cached.cached_at) <= self.cache_ttl_ms:
                return self._cached_result(cached, now)

            try:
                response = self.server.send_command(f"data get block {x} {y} {z}")
                nbt = parse_first_snbt_compound(response)
            except Exception as exc:  # noqa: BLE001
                if cached is not None and _age_ms(now, cached.cached_at) <= self.stale_if_error_ms:
                    result = self._cached_result(cached, now)
                    result["query_error"] = str(exc)
                    return result
                raise BlockEntityQueryError(
                    f"无法只读查询方块实体 {block_name}@({x},{y},{z}): {exc}"
                ) from exc

            observed_at_ms = int(self._wall_clock() * 1000)
            value = {
                "position": {"x": x, "y": y, "z": z},
                "block_name": str(block_name),
                "nbt": nbt,
                "sources": ["server_readonly"],
                "freshness": "live",
                "observed_at_ms": observed_at_ms,
                "age_ms": 0,
            }
            self._cache[key] = _CacheEntry(deepcopy(value), now, observed_at_ms)
            return value

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _cached_result(cached: _CacheEntry, now: float) -> dict[str, Any]:
        result = deepcopy(cached.value)
        result["freshness"] = "cached"
        result["age_ms"] = _age_ms(now, cached.cached_at)
        result["observed_at_ms"] = cached.observed_at_ms
        return result


def _position_key(position: Mapping[str, Any]) -> tuple[int, int, int]:
    try:
        return int(position["x"]), int(position["y"]), int(position["z"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid block position: {position!r}") from exc


def _age_ms(now: float, then: float) -> int:
    return max(0, int(round((now - then) * 1000)))


__all__ = [
    "BlockEntityProvider",
    "BlockEntityQueryError",
    "RconBlockEntityProvider",
]
