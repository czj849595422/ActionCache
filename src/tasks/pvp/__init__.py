"""PVP task utilities and two-team match runner."""

from importlib import import_module

from .schedule import (
    PVPDropConfig,
    PVPDropRecord,
    PVPSupplyError,
    PVP_SUPPLY_ITEM_NAMES,
    class_probabilities,
    gear_probability,
    generate_drop_batch,
    generate_drop_positions,
    generate_drop_record,
    summon_drop_command,
)
from .spawns import (
    DEFAULT_PVP_AGENT_SPAWN_SPACING,
    PVPSpawnError,
    generate_agent_spawn_positions,
    pvp_spec_with_seeded_spawns,
    pvp_task_data_with_seeded_spawns,
)

_RUNNER_EXPORTS = {
    "PVPMatchResult",
    "PVPMatchRunner",
    "PVPTeamRunConfig",
    "PVPTeamRunnerResult",
    "PVPTeamSessionProxy",
    "build_pvp_method_runner",
    "normalize_pvp_method_name",
    "pvp_matchup_id",
    "pvp_method_config",
    "pvp_method_names",
    "pvp_spec_with_team_size",
    "pvp_team_episode_ids",
}

_SEQUENTIAL_EXPORTS = {
    "PVPSequentialEpisodeResult",
    "PVPSequentialResult",
    "PVPSequentialRunner",
}


def __getattr__(name: str):
    if name in _SEQUENTIAL_EXPORTS:
        module = import_module(".sequential", __name__)
        return getattr(module, name)
    if name not in _RUNNER_EXPORTS:
        raise AttributeError(name)
    module = import_module(".runner", __name__)
    return getattr(module, name)

__all__ = [
    "PVPDropConfig",
    "PVPDropRecord",
    "PVPSpawnError",
    "PVPSupplyError",
    "PVP_SUPPLY_ITEM_NAMES",
    "DEFAULT_PVP_AGENT_SPAWN_SPACING",
    "class_probabilities",
    "gear_probability",
    "generate_agent_spawn_positions",
    "generate_drop_batch",
    "generate_drop_positions",
    "generate_drop_record",
    "pvp_spec_with_seeded_spawns",
    "pvp_task_data_with_seeded_spawns",
    "summon_drop_command",
    *_RUNNER_EXPORTS,
    *_SEQUENTIAL_EXPORTS,
]
