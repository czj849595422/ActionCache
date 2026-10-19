"""Runtime seed-based task spec generation for supported tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .boss_combat import (
    build_task_data as build_boss_combat_task_data,
    load_task_template as load_boss_combat_template,
)
from .mining import (
    build_task_data as build_mining_task_data,
    generation_spec_from_template as mining_generation_spec_from_template,
    load_generation_template as load_mining_template,
)
from .monster_clearance import (
    build_task_data as build_monster_clearance_task_data,
    generate_agent_spawn_positions as generate_monster_clearance_agent_positions,
    generation_spec_from_template as monster_clearance_generation_spec_from_template,
    load_generation_template as load_monster_clearance_template,
)
from .pvp.spawns import pvp_task_data_with_seeded_spawns
from .spec import TaskSpec, TaskSpecError, load_spec
from .trade import (
    build_task_data as build_trade_task_data,
    generate_trade_config,
    generation_spec_from_template as trade_generation_spec_from_template,
    load_generation_template as load_trade_template,
)


RUNTIME_SEEDED_TASK_IDS: tuple[str, ...] = (
    "boss_combat",
    "mining",
    "monster_clearance",
    "pvp",
    "trade",
)

_TASK_ROOT = Path(__file__).resolve().parent
def list_runtime_seeded_task_ids() -> tuple[str, ...]:
    return RUNTIME_SEEDED_TASK_IDS


def runtime_seeded_task_spec(task_id: str, seed: int) -> TaskSpec:
    """Expand one supported task into a deterministic runtime TaskSpec."""
    if not isinstance(seed, int):
        raise TaskSpecError("seed 必须是整数")
    if task_id == "mining":
        template = load_mining_template(_TASK_ROOT / "templates" / "mining.quick.yaml")
        generation_spec = mining_generation_spec_from_template(template)
        data = build_mining_task_data(
            template,
            generation_spec,
            task_id=task_id,
            seed=seed,
        )
        params = dict(data.get("params") or {})
        _drop_runtime_artifacts(params, ("schedule_file", "command_file"))
        data["params"] = params
        _set_generation_source(data, _TASK_ROOT / "templates" / "mining.quick.yaml")
    elif task_id == "monster_clearance":
        template = load_monster_clearance_template(
            _TASK_ROOT / "monster_clearance" / "template.yaml"
        )
        generation_spec = monster_clearance_generation_spec_from_template(template)
        by_agent = template["task"]["initial_state"]["by_agent"]
        agent_positions = generate_monster_clearance_agent_positions(
            generation_spec,
            seed,
            len(by_agent),
        )
        data = build_monster_clearance_task_data(
            template,
            generation_spec,
            task_id=task_id,
            seed=seed,
            agent_positions=agent_positions,
        )
        params = dict(data.get("params") or {})
        _drop_runtime_artifacts(params, ("schedule_file", "command_file"))
        data["params"] = params
        _set_generation_source(
            data,
            _TASK_ROOT / "monster_clearance" / "template.yaml",
        )
    elif task_id == "trade":
        template = load_trade_template(_TASK_ROOT / "templates" / "trade.quick.yaml")
        generation_spec = trade_generation_spec_from_template(template)
        config = generate_trade_config(generation_spec, seed)
        data = build_trade_task_data(template, task_id=task_id, seed=seed)
        params = dict(data.get("params") or {})
        _drop_runtime_artifacts(params, ("trade_config_file", "command_file"))
        params["trade_config"] = config.to_dict()
        params["generation_template_file"] = str(
            (_TASK_ROOT / "templates" / "trade.quick.yaml").resolve()
        )
        data["params"] = params
    elif task_id == "boss_combat":
        template = load_boss_combat_template(_TASK_ROOT / "specs" / "boss_combat.yaml")
        data = build_boss_combat_task_data(template, seed=seed)
        _set_generation_source(data, _TASK_ROOT / "specs" / "boss_combat.yaml")
    elif task_id == "pvp":
        base_spec = load_spec(_TASK_ROOT / "specs" / "pvp.yaml")
        data = pvp_task_data_with_seeded_spawns(
            base_spec.to_dict(),
            seed=seed,
        )
        params = dict(data.get("params") or {})
        params["generation_seed"] = seed
        data["params"] = params
        environment = dict(data.get("environment") or {})
        environment["seed"] = seed
        data["environment"] = environment
        metadata = dict(data.get("metadata") or {})
        metadata["generation_seed"] = seed
        metadata["runtime_seed_generation"] = True
        data["metadata"] = metadata
        name = str(data.get("name") or task_id)
        if "(seed " not in name:
            data["name"] = f"{name} (seed {seed})"
        _set_generation_source(data, _TASK_ROOT / "specs" / "pvp.yaml")
    else:
        raise TaskSpecError(f"不支持 runtime seed 生成的任务: {task_id}")

    params = dict(data.get("params") or {})
    params.setdefault("generation_seed", seed)
    params.setdefault("generation_template_file", _template_file_for(task_id))
    data["params"] = params
    metadata = dict(data.get("metadata") or {})
    metadata.setdefault("generation_seed", seed)
    metadata["runtime_seed_generation"] = True
    data["metadata"] = metadata
    spec = TaskSpec.from_dict(data)
    spec._source_path = _runtime_source_path(task_id, seed)
    return spec


def _drop_runtime_artifacts(target: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        target.pop(key, None)


def _set_generation_source(data: dict[str, Any], template_file: Path) -> None:
    params = dict(data.get("params") or {})
    params["generation_template_file"] = str(template_file.resolve())
    data["params"] = params


def _template_file_for(task_id: str) -> str:
    return str(
        {
            "boss_combat": _TASK_ROOT / "specs" / "boss_combat.yaml",
            "mining": _TASK_ROOT / "templates" / "mining.quick.yaml",
            "monster_clearance": _TASK_ROOT / "monster_clearance" / "template.yaml",
            "pvp": _TASK_ROOT / "specs" / "pvp.yaml",
            "trade": _TASK_ROOT / "templates" / "trade.quick.yaml",
        }[task_id].resolve()
    )


def _runtime_source_path(task_id: str, seed: int) -> Path:
    return (_TASK_ROOT / task_id / "config" / f"seed-{seed}.yaml").resolve()


__all__ = [
    "RUNTIME_SEEDED_TASK_IDS",
    "list_runtime_seeded_task_ids",
    "runtime_seeded_task_spec",
]
