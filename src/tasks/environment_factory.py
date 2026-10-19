"""统一创建一次性任务环境所需的 Server、Agent 与运行目录。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import tempfile
from typing import Any, Callable

from src.agents.action import SkillRegistry
from src.agents.skill_agent import SkillAgent
from src.launcher.mcrcon import load_server_port
from src.launcher.server import Server

from .manager import DEFAULT_AGENT_NAMES, TaskManager
from .spec import TaskSpec, TaskSpecError
from .world_workspace import TaskWorldWorkspace


TRADE_INTERNAL_ACTIONS = frozenset({"container.register_chests"})


# Action permissions are code-level task contracts.  Keep each task's set
# explicit so changing one task cannot silently change another task.
TASK_ACTION_SPACES: dict[str, frozenset[str] | None] = {
    "debug": None,
    "mining": frozenset(
        {
            "chat.say",
            "chat.whisper",
            "navigation.goto_position",
            "wait.wait",
            "mining.mine_diamond",
            "mining.mine_emerald",
            "mining.mine_gold",
            "mining.mine_iron",
            "mining.mine_lapis",
            "mining.mine_netherite",
            "equipment.equip_items",
            "equipment.unequip_items",
        }
    ),
    "monster_clearance": frozenset(
        {
            "chat.say",
            "chat.whisper",
            "navigation.goto_position",
            "wait.wait",
            "combat.observe",
            "combat.retreat",
            "combat.fight",
            "combat.use_food",
            "equipment.equip_items",
            "equipment.unequip_items",
            "inventory.pick",
            "inventory.drop",
            "inventory.give",
        }
    ),
    "boss_combat": frozenset(
        {
            "chat.say",
            "chat.whisper",
            "navigation.goto_position",
            "wait.wait",
            "combat.observe",
            "combat.fight",
            "combat.use_food",
            "equipment.equip_items",
            "equipment.unequip_items",
            "inventory.pick",
            "inventory.drop",
            "inventory.give",
        }
    ),
    "pvp": frozenset(
        {
            "chat.say",
            "chat.whisper",
            "navigation.goto_position",
            "wait.wait",
            "combat.observe",
            "combat.retreat",
            "combat.fight",
            "combat.use_food",
            "equipment.equip_items",
            "equipment.unequip_items",
            "inventory.pick",
            "inventory.drop",
            "inventory.give",
        }
    ),
    "trade": frozenset(
        {
            "chat.say",
            "chat.whisper",
            "wait.wait",
            "trade.inspect_own_containers",
            "trade.inspect_shop",
            "trade.transfer_owned_items",
            "trade.take_from_container",
            "trade.store_to_container",
            "trade.buy_from_shop",
            "trade.craft_target",
            "crafting.query_recipe",
        }
    ),
}


def task_action_space(spec: TaskSpec) -> frozenset[str] | None:
    """Return the code-defined action space for one task specification."""
    task_keys = (spec.task_id, spec.task_id.split(".", 1)[0])
    for key in task_keys:
        if key in TASK_ACTION_SPACES:
            return TASK_ACTION_SPACES[key]
    raise TaskSpecError(
        f"任务 {spec.task_id!r} 未定义动作空间，请在 environment_factory.py 中添加"
    )


def task_internal_actions(spec: TaskSpec) -> frozenset[str]:
    """返回只供可信任务 Manager 调用的 Skill 动作。"""
    if spec.category == "trade":
        return TRADE_INTERNAL_ACTIONS
    return frozenset()


def all_normal_skill_actions(
    *,
    internal_actions: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """发现全部普通 Skill 动作，并显式排除 Manager 内部动作。"""
    registry = SkillRegistry()
    actions = {
        f"{skill_name}.{command_name}"
        for skill_name, manifest in registry.manifests.items()
        for command_name in manifest.commands
    }
    return frozenset(actions - set(internal_actions))


def skill_agent_options(
    spec: TaskSpec,
    runtime_path: Path,
    registry_file: Path,
    *,
    unsafe_actions: bool = False,
) -> dict[str, Any]:
    """构造与任务一致的 bridge 环境变量和动作权限。"""
    bridge_env = {
        "CHEST_REGISTRY_FILE": str(registry_file.resolve()),
        "MC_TASK_CATEGORY": spec.category,
        "MC_OBSERVATION_RANGE": str(spec.observation_range),
    }
    if spec.category in {"pvp", "monster_clearance", "boss_combat"}:
        bridge_env["EQUIPMENT_REGISTRY_FILE"] = str(
            (runtime_path / "equipment_registry.json").resolve()
        )
    internal_actions = task_internal_actions(spec)
    if unsafe_actions:
        allowed_actions: frozenset[str] | None = all_normal_skill_actions(
            internal_actions=internal_actions
        )
    else:
        allowed_actions = task_action_space(spec)

    if spec.category == "trade":
        from .trade.config import TradeTaskConfig, load_trade_config, write_trade_config

        trade_runtime_dir = (runtime_path / "trade").resolve()
        trade_runtime_dir.mkdir(parents=True, exist_ok=True)
        config_value = (spec.params or {}).get("trade_config_file")
        raw_config = (spec.params or {}).get("trade_config")
        if isinstance(config_value, str) and config_value:
            trade_config_file = spec.resolve_resource(config_value).resolve()
            trade_config = load_trade_config(trade_config_file)
        elif isinstance(raw_config, dict):
            trade_config = TradeTaskConfig.from_dict(raw_config)
            trade_config_file = trade_runtime_dir / "trade_config.yaml"
            write_trade_config(trade_config, trade_config_file)
        else:
            raise TaskSpecError("Trade 任务缺少 params.trade_config_file 或 params.trade_config")
        queryable_items = {
            str(item).split(":", 1)[-1]
            for item in trade_config.base_price_map
        }
        queryable_items.update(
            str(item).split(":", 1)[-1] for item in trade_config.recipe_map
        )
        for recipe in trade_config.recipe_map.values():
            queryable_items.update(
                str(item).split(":", 1)[-1] for item, _ in recipe.ingredients
            )
        bridge_env.update(
            {
                "TRADE_CONFIG_FILE": str(trade_config_file),
                "TRADE_RUNTIME_DIR": str(trade_runtime_dir),
                "TRADE_QUERYABLE_ITEMS": json.dumps(sorted(queryable_items)),
            }
        )
    elif spec.category == "mining":
        ore_blocks = (spec.params or {}).get("ore_blocks")
        if not isinstance(ore_blocks, dict) or not ore_blocks:
            raise TaskSpecError("mining 缺少 params.ore_blocks")
        bridge_env["MINING_ORE_BLOCKS"] = ",".join(str(block) for block in ore_blocks)
    return {
        "bridge_env": bridge_env,
        "allowed_actions": allowed_actions,
        "internal_actions": internal_actions,
    }


@dataclass
class TaskEnvironmentBundle:
    """一次任务环境的已构造组件及其临时目录。"""

    spec: TaskSpec
    manager: TaskManager
    server: Any
    agents: list[Any]
    world_workspace: Any
    runtime_path: Path
    registry_file: Path
    _temporary_directory: tempfile.TemporaryDirectory[str]
    _runtime_closed: bool = False

    def close_runtime(self) -> None:
        if self._runtime_closed:
            return
        self._runtime_closed = True
        self._temporary_directory.cleanup()


ServerFactory = Callable[..., Any]
AgentFactory = Callable[..., Any]
WorkspaceFactory = Callable[..., Any]


def create_task_environment_bundle(
    *,
    spec: TaskSpec,
    manager: TaskManager,
    source_world: Path,
    server_dir: Path,
    bridge_port_start: int = 3201,
    log_dir: Path | None = None,
    runtime_root: Path | None = None,
    unsafe_actions: bool = False,
    server_factory: ServerFactory = Server,
    agent_factory: AgentFactory = SkillAgent,
    workspace_factory: WorkspaceFactory = TaskWorldWorkspace,
) -> TaskEnvironmentBundle:
    """创建任务组件；实际启动和清理由 TaskEnvironmentSession 负责。"""
    root = Path(runtime_root or Path.cwd() / ".tmp").resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary_directory = tempfile.TemporaryDirectory(
        prefix="task_runtime_",
        dir=root,
    )
    try:
        runtime_path = Path(temporary_directory.name).resolve()
        registry_file = runtime_path / "chests.json"
        server_port = load_server_port(server_dir)
        options = skill_agent_options(
            spec,
            runtime_path,
            registry_file,
            unsafe_actions=unsafe_actions,
        )
        server = server_factory(server_dir=server_dir, log_dir=log_dir)
        workspace = workspace_factory(
            source_world=source_world,
            server_dir=server_dir,
        )
        agent_count = int(spec.agents["count"])
        names = spec.agents.get("names") or list(
            DEFAULT_AGENT_NAMES[:agent_count]
        )
        agents = [
            agent_factory(
                username=name,
                port=server_port,
                bridge_port=bridge_port_start + index,
                log_dir=log_dir,
                **options,
            )
            for index, name in enumerate(names)
        ]
        return TaskEnvironmentBundle(
            spec=spec,
            manager=manager,
            server=server,
            agents=agents,
            world_workspace=workspace,
            runtime_path=runtime_path,
            registry_file=registry_file,
            _temporary_directory=temporary_directory,
        )
    except BaseException:
        temporary_directory.cleanup()
        raise
