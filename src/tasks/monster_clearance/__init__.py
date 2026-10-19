"""动态怪物清剿任务的生成、解析和转换 API。"""

from .generator import (
    build_task_data,
    TaskBundlePaths,
    generate_agent_spawn_positions,
    generate_task_batch,
    generation_spec_from_template,
    load_generation_template,
)
from .schedule import (
    EncounterBounds,
    EncounterSpawnRecord,
    MonsterClearanceGenerationSpec,
    MonsterClearanceScheduleError,
    batch_plans_equal,
    command_file_text,
    generate_spawn_schedule,
    load_spawn_schedule,
    schedule_to_batch_plan,
    spawn_schedule_text,
    validate_spawn_schedule,
    write_command_file,
    write_spawn_schedule,
)

__all__ = [
    "EncounterBounds",
    "EncounterSpawnRecord",
    "MonsterClearanceGenerationSpec",
    "MonsterClearanceScheduleError",
    "build_task_data",
    "TaskBundlePaths",
    "batch_plans_equal",
    "command_file_text",
    "generate_agent_spawn_positions",
    "generate_spawn_schedule",
    "generate_task_batch",
    "generation_spec_from_template",
    "load_generation_template",
    "load_spawn_schedule",
    "schedule_to_batch_plan",
    "spawn_schedule_text",
    "validate_spawn_schedule",
    "write_command_file",
    "write_spawn_schedule",
]
