"""Boss combat task helpers."""

from .generator import (
    BossCombatGenerationSpec,
    TaskBundlePaths,
    build_task_data,
    generate_agent_spawn_positions,
    generate_boss_initial_loadout,
    generate_boss_spawn_position,
    generate_task_batch,
    generation_spec_from_task,
    load_task_template,
)

__all__ = [
    "BossCombatGenerationSpec",
    "TaskBundlePaths",
    "build_task_data",
    "generate_agent_spawn_positions",
    "generate_boss_initial_loadout",
    "generate_boss_spawn_position",
    "generate_task_batch",
    "generation_spec_from_task",
    "load_task_template",
]
