"""Boss combat arena layout and spawn bounds."""

from __future__ import annotations

from collections.abc import Mapping


# The wall occupies the outermost block on each side. Keep the Boss center one
# block inside it so the initial entity is not summoned into obsidian.
BOSS_ARENA_WALL_COMMANDS: tuple[str, ...] = (
    "fill -50 0 -50 -50 30 50 minecraft:obsidian",
    "fill 50 0 -50 50 30 50 minecraft:obsidian",
    "fill -50 0 -50 50 30 -50 minecraft:obsidian",
    "fill -50 0 50 50 30 50 minecraft:obsidian",
)
BOSS_SPAWN_MIN_X = -49.0
BOSS_SPAWN_MAX_X = 49.0
BOSS_SPAWN_MIN_Y = 1.0
BOSS_SPAWN_MAX_Y = 29.0
BOSS_SPAWN_MIN_Z = -49.0
BOSS_SPAWN_MAX_Z = 49.0


def is_boss_spawn_inside(position: Mapping[str, object]) -> bool:
    """Return whether a Boss center is inside the obsidian walls."""
    try:
        x = float(position["x"])
        y = float(position["y"])
        z = float(position["z"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        BOSS_SPAWN_MIN_X <= x <= BOSS_SPAWN_MAX_X
        and BOSS_SPAWN_MIN_Y <= y <= BOSS_SPAWN_MAX_Y
        and BOSS_SPAWN_MIN_Z <= z <= BOSS_SPAWN_MAX_Z
    )


__all__ = [
    "BOSS_ARENA_WALL_COMMANDS",
    "BOSS_SPAWN_MAX_X",
    "BOSS_SPAWN_MAX_Y",
    "BOSS_SPAWN_MAX_Z",
    "BOSS_SPAWN_MIN_X",
    "BOSS_SPAWN_MIN_Y",
    "BOSS_SPAWN_MIN_Z",
    "is_boss_spawn_inside",
]
