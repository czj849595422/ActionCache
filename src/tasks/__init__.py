"""任务管理模块：任务规格（YAML）、注册表、任务管理器协议与示例实现。"""

from pathlib import Path

from .recipe_dag import (
    CraftSimulationResult,
    ItemRequirement,
    RecipeCraftStep,
    RecipeDAG,
    RecipeDefinition,
    RecipeGraphError,
    RecipePlan,
)
from .mineflayer_recipes import (
    DEFAULT_MINECRAFT_VERSION,
    MineflayerRecipe,
    MineflayerRecipeCatalog,
    MineflayerRecipeError,
    load_mineflayer_recipe_catalog,
    select_mineflayer_recipes,
)
from .environment_factory import (
    TASK_ACTION_SPACES,
    TRADE_INTERNAL_ACTIONS,
    TaskEnvironmentBundle,
    all_normal_skill_actions,
    create_task_environment_bundle,
    skill_agent_options,
    task_action_space,
    task_internal_actions,
)
from .environment_runner import (
    EnvironmentAgent,
    EnvironmentServer,
    EnvironmentWorkspace,
    TaskEnvironmentPoll,
    TaskEnvironmentRunner,
    TaskEnvironmentRunResult,
    TaskEnvironmentSession,
)
from .manager import (
    BaseTaskManager,
    EvaluatorContext,
    TaskManager,
    TaskOutcome,
    TaskStatus,
)
from .system_messages import SystemMessageWorker
from .runtime_config import apply_task_runtime_overrides
from .managers.boss_combat import BossCombatManager
from .managers.mining import MiningManager
from .managers.monster_clearance import MonsterClearanceManager
from .managers.pvp import PVPManager
from .managers.trade import TradeTaskManager
from .boss_combat import (
    BossCombatGenerationSpec,
    generate_boss_spawn_position,
    generation_spec_from_task as boss_combat_generation_spec_from_task,
)
from .boss_combat import generate_task_batch as generate_boss_combat_task_batch
from .mining import (
    MiningBounds,
    MiningGenerationSpec,
    MiningScheduleError,
    OreChoice,
    OreSpawnRecord,
    generate_spawn_schedule,
    generate_task_batch,
    load_spawn_schedule,
    schedule_to_batch_plan,
    write_command_file,
)
from .registry import TaskRegistry, TaskRegistryError
from .trade import (
    CraftingStep,
    SnbtParseError,
    TradeAgentConfig,
    TradeConfigError,
    TradeCraftStep,
    TradeDependency,
    TradeGenerationSpec,
    TradeProfessionConfig,
    TradePlan,
    TradeRecipe,
    TradeTarget,
    TradeTaskConfig,
    TradeTransaction,
    TradeVerificationResult,
    calculate_prices,
    find_trade_plans,
    generate_trade_config,
    load_trade_config,
    parse_chest_items,
    trade_config_to_prepare_plan,
)
from .trade import generate_task_batch as generate_trade_task_batch
from .spec import (
    FAILURE_TYPES,
    SUCCESS_TYPES,
    TaskSpec,
    TaskSpecError,
    load_spec,
    task_description_text,
)
from .seed_presets import (
    DEFAULT_SEED_PRESETS_FILE,
    TaskSeedPresetError,
    list_seed_preset_ids,
    parse_seeded_task_arg,
    seeded_task_spec,
)
from .task_configs import (
    SPECS_DIR,
    TASKS_DIR,
    TaskConfigError,
    expand_task_args,
    list_task_configs,
    task_config_dir,
)
from .world_workspace import (
    TaskWorldError,
    TaskWorldWorkspace,
    resolve_task_world,
)


def create_default_registry() -> TaskRegistry:
    """创建内置注册表：加载 specs/*.yaml 并注册内置类别管理器。"""
    registry = TaskRegistry()
    registry.register_manager("debug", BaseTaskManager)
    registry.register_manager("boss_combat", BossCombatManager)
    registry.register_manager("mining", MiningManager)
    registry.register_manager("monster_clearance", MonsterClearanceManager)
    registry.register_manager("pvp", PVPManager)
    registry.register_manager("trade", TradeTaskManager)
    specs_dir = Path(__file__).resolve().parent / "specs"
    for path in sorted(specs_dir.glob("*.yaml")):
        spec = load_spec(path)
        if spec.category == "pvp":
            from .pvp.spawns import pvp_spec_with_seeded_spawns

            spec = pvp_spec_with_seeded_spawns(spec)
        registry.register_spec(spec)
    return registry


__all__ = [
    "BaseTaskManager",
    "BossCombatManager",
    "BossCombatGenerationSpec",
    "CraftSimulationResult",
    "EnvironmentAgent",
    "EnvironmentServer",
    "EnvironmentWorkspace",
    "EvaluatorContext",
    "FAILURE_TYPES",
    "MiningBounds",
    "MiningManager",
    "MiningGenerationSpec",
    "MiningScheduleError",
    "MineflayerRecipe",
    "MineflayerRecipeCatalog",
    "MineflayerRecipeError",
    "MonsterClearanceManager",
    "PVPManager",
    "ItemRequirement",
    "OreChoice",
    "OreSpawnRecord",
    "RecipeCraftStep",
    "RecipeDAG",
    "RecipeDefinition",
    "RecipeGraphError",
    "RecipePlan",
    "SUCCESS_TYPES",
    "TaskManager",
    "TaskEnvironmentRunner",
    "TaskEnvironmentRunResult",
    "TaskEnvironmentPoll",
    "TaskEnvironmentSession",
    "TaskEnvironmentBundle",
    "TaskOutcome",
    "TaskRegistry",
    "TaskRegistryError",
    "TaskSpec",
    "TaskSpecError",
    "TaskStatus",
    "SystemMessageWorker",
    "apply_task_runtime_overrides",
    "TaskConfigError",
    "TaskSeedPresetError",
    "TaskWorldError",
    "TaskWorldWorkspace",
    "CraftingStep",
    "TradeCraftStep",
    "TradeDependency",
    "SnbtParseError",
    "TradeAgentConfig",
    "TradeConfigError",
    "TradeGenerationSpec",
    "TradeProfessionConfig",
    "TradePlan",
    "TradeRecipe",
    "TradeTarget",
    "TradeTaskConfig",
    "TradeTransaction",
    "TradeVerificationResult",
    "TradeTaskManager",
    "calculate_prices",
    "create_default_registry",
    "expand_task_args",
    "generate_spawn_schedule",
    "generate_boss_combat_task_batch",
    "generate_boss_spawn_position",
    "generate_task_batch",
    "generate_trade_config",
    "find_trade_plans",
    "generate_trade_task_batch",
    "load_spawn_schedule",
    "load_spec",
    "list_task_configs",
    "load_mineflayer_recipe_catalog",
    "load_trade_config",
    "parse_chest_items",
    "resolve_task_world",
    "schedule_to_batch_plan",
    "trade_config_to_prepare_plan",
    "select_mineflayer_recipes",
    "DEFAULT_MINECRAFT_VERSION",
    "boss_combat_generation_spec_from_task",
    "write_command_file",
    "TRADE_INTERNAL_ACTIONS",
    "TASK_ACTION_SPACES",
    "all_normal_skill_actions",
    "create_task_environment_bundle",
    "skill_agent_options",
    "task_action_space",
    "task_internal_actions",
    "task_config_dir",
    "task_description_text",
    "seeded_task_spec",
    "parse_seeded_task_arg",
    "list_seed_preset_ids",
    "TASKS_DIR",
    "SPECS_DIR",
    "DEFAULT_SEED_PRESETS_FILE",
]
