"""COAP 缓存模块配置（同步版本）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfigError(ValueError):
    pass


class CacheBootMode(str, Enum):
    AUTO = "auto"
    EMPTY = "empty"
    LLM_INIT = "llm_init"
    REUSE_PREVIOUS = "reuse_previous"
    REUSE_OLD = "reuse_old"


class CacheColdStartMode(str, Enum):
    EMPTY = "empty"
    LLM_INIT = "llm_init"


class CacheSharingMode(str, Enum):
    SHARED = "shared"
    PER_AGENT = "per_agent"


class CacheParameterMode(str, Enum):
    PRESERVE = "preserve"
    STRIP = "strip"


class TieBreakPolicy(str, Enum):
    STABLE = "stable"
    SCORE_THEN_RECENCY = "score_then_recency"
    SCORE_THEN_SUCCESS_RATE = "score_then_success_rate"


class ReasonSamplingPolicy(str, Enum):
    ALL = "all"
    LATEST = "latest"
    DIVERSE = "diverse"
    RANDOM = "random"


class BufferOverflowPolicy(str, Enum):
    ERROR = "error"
    DROP_OLDEST = "drop_oldest"


class CacheActionCandidatePolicy(str, Enum):
    STRICT = "strict"
    ADVISORY = "advisory"


class ScoreStrategy(str, Enum):
    BAYESIAN_SUCCESS_RATE = "bayesian_success_rate"
    CONFIDENCE_ADJUSTED = "confidence_adjusted"
    EXPONENTIAL_SMOOTHING = "exponential_smoothing"
    INCREMENTAL_FEEDBACK = "incremental_feedback"
    BOUNDED_ADDITIVE = "bounded_additive"
    FREQUENCY_AWARE = "frequency_aware"


DEFAULT_ACTION_READY_KEYWORD = "</action>"


DEFAULT_IMPORTANT_PARAMETERS: dict[str, tuple[str, ...]] = {
    # block
    "block.inspect": ("position",),
    "block.break": ("position",),
    "block.mine": ("position",),
    "block.place": ("item", "position"),
    "block.inspect_region": ("from", "to"),
    "block.break_region": ("from", "to"),
    "block.place_region": ("item", "from", "to"),
    # chat
    "chat.say": (),
    "chat.whisper": ("username",),
    # combat
    "combat.patrol": (),
    "combat.observe": (),
    "combat.attack": ("target",),
    "combat.retreat": ("target",),
    "combat.fight_using_weapon": ("target",),
    "combat.fight_using_bow": ("target",),
    "combat.fight": ("target",),
    "combat.use_food": ("food",),
    # container
    "container.inspect_chest": ("position", "chest", "chest_id", "chest_name"),
    "container.pick_object_from_chest": (
        "position",
        "chest",
        "chest_id",
        "chest_name",
    ),
    "container.place_object_to_chest": (
        "position",
        "chest",
        "chest_id",
        "chest_name",
    ),
    "container.register_chests": ("names",),
    "container.list_chests": (),
    # crafting
    "crafting.query_recipe": ("item",),
    "crafting.craft_with_recipe": ("item", "recipe_index"),
    "crafting.craft_item": ("item",),
    # demo fallback actions
    "demo_combat.lock_nearest_hostile": (),
    "demo_survival.flee_nearest_hostile": (),
    # equipment
    "equipment.equip_item": ("item", "destination"),
    "equipment.unequip_item": ("destination",),
    "equipment.auto_equip_combat": (),
    "equipment.equip_items": ("items[].name",),
    "equipment.unequip_items": ("destinations",),
    # farm
    "farm.harvest": ("crop",),
    # furnace
    "furnace.find_nearby": (),
    "furnace.open": ("position",),
    "furnace.put_fuel": (),
    "furnace.put_input": ("position", "item"),
    "furnace.take_fuel": ("position",),
    "furnace.take_input": ("position",),
    "furnace.take_output": ("position",),
    "furnace.smelt_item": ("position", "item"),
    "furnace.close": (),
    # inventory
    "inventory.list": (),
    "inventory.inspect": (),
    "inventory.count_item": ("name",),
    "inventory.has_item": ("name",),
    "inventory.equip": ("name",),
    "inventory.pick": ("positions",),
    "inventory.drop": ("items[].name",),
    "inventory.give": ("target", "items[].name"),
    # mining
    "mining.mine_lapis": ("position",),
    "mining.mine_iron": ("position",),
    "mining.mine_gold": ("position",),
    "mining.mine_emerald": ("position",),
    "mining.mine_diamond": ("position",),
    "mining.mine_netherite": ("position",),
    # navigation
    "navigation.goto_position": (
        "position",
        "entity_id",
        "entity_uuid",
        "entity_name",
    ),
    "navigation.follow_position": ("position",),
    "navigation.follow_entity": ("entity_id", "entity_uuid", "entity_name"),
    "navigation.goto_block": ("block_name",),
    "navigation.explore": ("target",),
    # survival
    "survival.use_food": ("food",),
    # trade
    "trade.inspect_own_containers": (),
    "trade.inspect_shop": ("shop_name",),
    "trade.transfer_owned_items": ("container", "direction"),
    "trade.take_from_container": ("container",),
    "trade.store_to_container": ("container",),
    "trade.buy_from_shop": ("shop_name", "item"),
    "trade.craft_target": ("item",),
    # wait
    "wait.wait": (),
}


def default_important_parameters() -> dict[str, tuple[str, ...]]:
    return dict(DEFAULT_IMPORTANT_PARAMETERS)


def _coerce_enum(value: Any, enum_type: type[Enum], label: str):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ConfigError(f"{label} 非法: {value!r}") from exc


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} 必须是布尔值")
    return value


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} 必须是整数")
    if value < minimum:
        raise ConfigError(f"{label} 必须 >= {minimum}")
    return value


def _require_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} 必须是数字")
    value = float(value)
    if not 0 <= value <= 1:
        raise ConfigError(f"{label} 必须在 [0, 1] 内")
    return value


def _require_positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} 必须是数字")
    value = float(value)
    if value <= 0:
        raise ConfigError(f"{label} 必须大于 0")
    return value


@dataclass(frozen=True)
class CacheBootConfig:
    """缓存初始化配置。"""

    mode: CacheBootMode = CacheBootMode.LLM_INIT
    source_path: str | None = None
    cold_start_mode: CacheColdStartMode = CacheColdStartMode.LLM_INIT
    target_entry_count: int = 10
    initialization_call_budget: int = 1
    initialization_token_budget: int = 16_000
    filter_unknown_actions: bool = True
    normalize_loaded_scores: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce_enum(self.mode, CacheBootMode, "mode"))
        object.__setattr__(
            self,
            "cold_start_mode",
            _coerce_enum(self.cold_start_mode, CacheColdStartMode, "cold_start_mode"),
        )
        if self.source_path is not None and (
            not isinstance(self.source_path, str) or not self.source_path.strip()
        ):
            raise ConfigError("source_path 必须是非空字符串或 None")
        object.__setattr__(
            self,
            "target_entry_count",
            _require_int(self.target_entry_count, "target_entry_count", minimum=0),
        )
        object.__setattr__(
            self,
            "initialization_call_budget",
            _require_int(
                self.initialization_call_budget,
                "initialization_call_budget",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "initialization_token_budget",
            _require_int(
                self.initialization_token_budget,
                "initialization_token_budget",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "filter_unknown_actions",
            _require_bool(self.filter_unknown_actions, "filter_unknown_actions"),
        )
        object.__setattr__(
            self,
            "normalize_loaded_scores",
            _require_bool(self.normalize_loaded_scores, "normalize_loaded_scores"),
        )
        if self.mode in {
            CacheBootMode.EMPTY,
            CacheBootMode.LLM_INIT,
            CacheBootMode.REUSE_PREVIOUS,
        } and (
            self.source_path is not None
        ):
            raise ConfigError("source_path 仅在 auto 或 reuse_old 模式有效")
        if (
            self.mode
            in {CacheBootMode.LLM_INIT, CacheBootMode.REUSE_PREVIOUS}
            or (
                self.mode is CacheBootMode.AUTO
                and self.source_path is None
                and self.cold_start_mode is CacheColdStartMode.LLM_INIT
            )
        ):
            for label, value in (
                ("target_entry_count", self.target_entry_count),
                ("initialization_call_budget", self.initialization_call_budget),
                ("initialization_token_budget", self.initialization_token_budget),
            ):
                if value == 0:
                    raise ConfigError(f"{label} 在 llm_init 模式必须大于 0")
        if self.mode is CacheBootMode.REUSE_OLD and self.source_path is None:
            raise ConfigError("reuse_old 模式必须提供 source_path")

    @property
    def resolved_mode(self) -> CacheBootMode:
        if self.mode is not CacheBootMode.AUTO:
            return self.mode
        if self.source_path is not None:
            return CacheBootMode.REUSE_OLD
        return (
            CacheBootMode.LLM_INIT
            if self.cold_start_mode is CacheColdStartMode.LLM_INIT
            else CacheBootMode.EMPTY
        )


@dataclass(frozen=True)
class CacheRepresentationConfig:
    """缓存条目的参数表示方式。"""

    parameter_mode: CacheParameterMode = CacheParameterMode.STRIP

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameter_mode",
            _coerce_enum(
                self.parameter_mode, CacheParameterMode, "parameter_mode"
            ),
        )


@dataclass(frozen=True)
class RetrievalConfig:
    """缓存检索配置。

    前序动作默认只按动作名匹配（忽略参数），使
    ``f(a1) -> F(a1)`` 与 ``f(b1) -> F(b1)`` 可共享同一规律；
    参数级差异由双路一致性（重要参数对比）负责，不参与检索匹配。
    """

    top_k: int = 5
    minimum_score: float = 0.0
    tie_break_policy: TieBreakPolicy = TieBreakPolicy.STABLE
    require_exact_previous_action: bool = False
    allow_start_sentinel: bool = True
    start_action_name: str = "START"

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_k", _require_int(self.top_k, "top_k", minimum=1))
        object.__setattr__(
            self,
            "minimum_score",
            _require_probability(self.minimum_score, "minimum_score"),
        )
        object.__setattr__(
            self,
            "tie_break_policy",
            _coerce_enum(self.tie_break_policy, TieBreakPolicy, "tie_break_policy"),
        )
        object.__setattr__(
            self,
            "require_exact_previous_action",
            _require_bool(
                self.require_exact_previous_action,
                "require_exact_previous_action",
            ),
        )
        object.__setattr__(
            self,
            "allow_start_sentinel",
            _require_bool(self.allow_start_sentinel, "allow_start_sentinel"),
        )
        if (
            not isinstance(self.start_action_name, str)
            or not self.start_action_name.strip()
        ):
            raise ConfigError("start_action_name 必须是非空字符串")


@dataclass(frozen=True)
class BufferUpdateConfig:
    """候选缓冲区与内容更新配置。"""

    buffer_size_threshold: int = 5
    maximum_buffer_records: int = 1_000
    buffer_overflow_policy: BufferOverflowPolicy = BufferOverflowPolicy.ERROR
    update_interval_seconds: float = 30.0
    maximum_reasons_per_pair: int = 5
    reason_sampling_policy: ReasonSamplingPolicy = ReasonSamplingPolicy.DIVERSE
    default_uncertain_score: float = 0.5
    deduplicate_by_decision_id: bool = True
    environment_source_preferred: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "buffer_size_threshold",
            _require_int(self.buffer_size_threshold, "buffer_size_threshold", minimum=1),
        )
        object.__setattr__(
            self,
            "maximum_buffer_records",
            _require_int(
                self.maximum_buffer_records, "maximum_buffer_records", minimum=1
            ),
        )
        if self.maximum_buffer_records < self.buffer_size_threshold:
            raise ConfigError(
                "maximum_buffer_records 不能小于 buffer_size_threshold"
            )
        object.__setattr__(
            self,
            "buffer_overflow_policy",
            _coerce_enum(
                self.buffer_overflow_policy,
                BufferOverflowPolicy,
                "buffer_overflow_policy",
            ),
        )
        object.__setattr__(
            self,
            "update_interval_seconds",
            _require_positive_number(
                self.update_interval_seconds, "update_interval_seconds"
            ),
        )
        object.__setattr__(
            self,
            "maximum_reasons_per_pair",
            _require_int(
                self.maximum_reasons_per_pair, "maximum_reasons_per_pair", minimum=1
            ),
        )
        object.__setattr__(
            self,
            "reason_sampling_policy",
            _coerce_enum(
                self.reason_sampling_policy,
                ReasonSamplingPolicy,
                "reason_sampling_policy",
            ),
        )
        object.__setattr__(
            self,
            "default_uncertain_score",
            _require_probability(
                self.default_uncertain_score, "default_uncertain_score"
            ),
        )
        object.__setattr__(
            self,
            "deduplicate_by_decision_id",
            _require_bool(
                self.deduplicate_by_decision_id, "deduplicate_by_decision_id"
            ),
        )
        object.__setattr__(
            self,
            "environment_source_preferred",
            _require_bool(
                self.environment_source_preferred, "environment_source_preferred"
            ),
        )


@dataclass(frozen=True)
class ScoreConfig:
    """缓存评分配置（初始分下限/冷启动上限 + 周期递推参数）。"""

    strategy: ScoreStrategy = ScoreStrategy.BAYESIAN_SUCCESS_RATE
    lambda_value: float = 5.0
    alpha: float = 0.3
    success_reward: float = 0.1
    failure_penalty: float = 0.1
    idle_decay: float = 0.0
    minimum_llm_initial_score: float = 0.05
    maximum_llm_initial_score: float = 0.5
    minimum_score: float = 0.0
    maximum_score: float = 1.0
    update_interval_seconds: float = 30.0
    minimum_feedback_count: int = 5
    epsilon: float = 1e-9
    clip_scores: bool = True
    reset_interval_counts: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "strategy", _coerce_enum(self.strategy, ScoreStrategy, "strategy")
        )
        object.__setattr__(
            self,
            "lambda_value",
            _require_positive_number(self.lambda_value, "lambda_value"),
        )
        alpha = self.alpha
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
            raise ConfigError("alpha 必须是数字")
        alpha = float(alpha)
        if not 0 < alpha <= 1:
            raise ConfigError("alpha 必须在 (0, 1] 内")
        object.__setattr__(self, "alpha", alpha)
        object.__setattr__(
            self,
            "success_reward",
            _require_probability(self.success_reward, "success_reward"),
        )
        object.__setattr__(
            self,
            "failure_penalty",
            _require_probability(self.failure_penalty, "failure_penalty"),
        )
        idle_decay = self.idle_decay
        if isinstance(idle_decay, bool) or not isinstance(idle_decay, (int, float)):
            raise ConfigError("idle_decay 必须是数字")
        idle_decay = float(idle_decay)
        if not 0 <= idle_decay < 1:
            raise ConfigError("idle_decay 必须在 [0, 1) 内")
        object.__setattr__(self, "idle_decay", idle_decay)
        object.__setattr__(
            self,
            "minimum_llm_initial_score",
            _require_probability(
                self.minimum_llm_initial_score, "minimum_llm_initial_score"
            ),
        )
        object.__setattr__(
            self,
            "maximum_llm_initial_score",
            _require_probability(
                self.maximum_llm_initial_score, "maximum_llm_initial_score"
            ),
        )
        if not 0 < self.maximum_llm_initial_score <= 0.5:
            raise ConfigError("maximum_llm_initial_score 必须在 (0, 0.5] 内")
        object.__setattr__(
            self,
            "minimum_score",
            _require_probability(self.minimum_score, "minimum_score"),
        )
        object.__setattr__(
            self,
            "maximum_score",
            _require_probability(self.maximum_score, "maximum_score"),
        )
        if self.minimum_score >= self.maximum_score:
            raise ConfigError("minimum_score 必须小于 maximum_score")
        if not (
            self.minimum_score
            <= self.minimum_llm_initial_score
            <= self.maximum_score
        ):
            raise ConfigError("minimum_llm_initial_score 必须在评分边界内")
        if not (
            self.minimum_llm_initial_score
            <= self.maximum_llm_initial_score
            <= self.maximum_score
        ):
            raise ConfigError("maximum_llm_initial_score 必须在评分边界内")
        object.__setattr__(
            self,
            "update_interval_seconds",
            _require_positive_number(
                self.update_interval_seconds, "update_interval_seconds"
            ),
        )
        object.__setattr__(
            self,
            "minimum_feedback_count",
            _require_int(
                self.minimum_feedback_count, "minimum_feedback_count", minimum=1
            ),
        )
        object.__setattr__(
            self,
            "epsilon",
            _require_positive_number(self.epsilon, "epsilon"),
        )
        object.__setattr__(
            self, "clip_scores", _require_bool(self.clip_scores, "clip_scores")
        )
        object.__setattr__(
            self,
            "reset_interval_counts",
            _require_bool(self.reset_interval_counts, "reset_interval_counts"),
        )


@dataclass(frozen=True)
class COAPConfig:
    """COAP runner 运行时配置（独立于 PPAConfig，不改 PPA 流程）。"""

    language: str = "zh"
    observation_interval: float = 1.0
    planning_interval: float = 5.0
    cache_planning_enabled: bool = True
    timed_planning_enabled: bool = True
    standard_verification_enabled: bool = True
    cache_reasoning_optimization_enabled: bool = True
    score_update_enabled: bool = True
    acting_poll_interval: float = 0.2
    task_poll_interval: float = 0.2
    initial_observation_timeout: float = 10.0
    worker_stop_timeout: float = 5.0
    max_consecutive_errors: int = 3
    chat_window: int | None = 20
    max_run_seconds: float | None = None
    max_llm_calls: int | None = None
    max_llm_calls_per_agent: int | None = None
    broadcast_results: bool = True
    system_messages_enabled: bool = False
    system_message_interval: float = 30.0
    enable_chat_actions: bool = False
    action_ready_keyword: str = DEFAULT_ACTION_READY_KEYWORD
    cache_sharing: CacheSharingMode = CacheSharingMode.SHARED
    cache_boot: CacheBootConfig = field(default_factory=CacheBootConfig)
    cache_buffer: BufferUpdateConfig = field(default_factory=BufferUpdateConfig)
    cache_retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    cache_score: ScoreConfig = field(default_factory=ScoreConfig)
    cache_representation: CacheRepresentationConfig = field(
        default_factory=CacheRepresentationConfig
    )
    cache_action_timeout_seconds: float = 15.0
    standard_action_timeout_seconds: float = 60.0
    maximum_replans: int = 3
    candidate_policy: CacheActionCandidatePolicy = (
        CacheActionCandidatePolicy.ADVISORY
    )
    important_parameters: dict[str, tuple[str, ...]] = field(
        default_factory=default_important_parameters
    )

    def __post_init__(self) -> None:
        if self.language not in {"zh", "en"}:
            raise ConfigError("language 必须是 'zh' 或 'en'")
        for label in (
            "cache_planning_enabled",
            "timed_planning_enabled",
            "standard_verification_enabled",
            "cache_reasoning_optimization_enabled",
            "score_update_enabled",
        ):
            object.__setattr__(
                self,
                label,
                _require_bool(getattr(self, label), label),
            )
        if not self.cache_planning_enabled and not self.standard_verification_enabled:
            raise ConfigError(
                "cache_planning_enabled 和 standard_verification_enabled 不能同时关闭"
            )
        object.__setattr__(
            self,
            "planning_interval",
            _require_positive_number(self.planning_interval, "planning_interval"),
        )
        standard_verification_enabled = self.standard_verification_enabled
        timed_planning_enabled = self.timed_planning_enabled
        if not standard_verification_enabled:
            timed_planning_enabled = False
        object.__setattr__(
            self,
            "timed_planning_enabled",
            timed_planning_enabled,
        )
        for label in (
            "observation_interval",
            "acting_poll_interval",
            "task_poll_interval",
            "initial_observation_timeout",
            "worker_stop_timeout",
        ):
            object.__setattr__(
                self, label, _require_positive_number(getattr(self, label), label)
            )
        object.__setattr__(
            self,
            "max_consecutive_errors",
            _require_int(
                self.max_consecutive_errors, "max_consecutive_errors", minimum=1
            ),
        )
        if self.chat_window is not None and self.chat_window < 1:
            raise ConfigError("chat_window 必须为正数")
        if self.max_run_seconds is not None and self.max_run_seconds <= 0:
            raise ConfigError("max_run_seconds 必须为正数")
        if self.max_llm_calls is not None and self.max_llm_calls < 1:
            raise ConfigError("max_llm_calls 必须 >= 1")
        if (
            self.max_llm_calls_per_agent is not None
            and self.max_llm_calls_per_agent < 1
        ):
            raise ConfigError("max_llm_calls_per_agent 必须 >= 1")
        object.__setattr__(
            self,
            "broadcast_results",
            _require_bool(self.broadcast_results, "broadcast_results"),
        )
        object.__setattr__(
            self,
            "system_messages_enabled",
            _require_bool(
                self.system_messages_enabled,
                "system_messages_enabled",
            ),
        )
        object.__setattr__(
            self,
            "system_message_interval",
            _require_positive_number(
                self.system_message_interval,
                "system_message_interval",
            ),
        )
        object.__setattr__(
            self,
            "enable_chat_actions",
            _require_bool(self.enable_chat_actions, "enable_chat_actions"),
        )
        if (
            not isinstance(self.action_ready_keyword, str)
            or not self.action_ready_keyword.strip()
        ):
            raise ConfigError("action_ready_keyword 必须是非空字符串")
        object.__setattr__(
            self,
            "cache_sharing",
            _coerce_enum(self.cache_sharing, CacheSharingMode, "cache_sharing"),
        )
        object.__setattr__(
            self,
            "candidate_policy",
            _coerce_enum(
                self.candidate_policy,
                CacheActionCandidatePolicy,
                "candidate_policy",
            ),
        )
        object.__setattr__(
            self,
            "cache_action_timeout_seconds",
            _require_positive_number(
                self.cache_action_timeout_seconds,
                "cache_action_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "standard_action_timeout_seconds",
            _require_positive_number(
                self.standard_action_timeout_seconds,
                "standard_action_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "maximum_replans",
            _require_int(self.maximum_replans, "maximum_replans", minimum=0),
        )
        object.__setattr__(
            self,
            "important_parameters",
            _normalize_important_parameters(self.important_parameters),
        )


def _normalize_important_parameters(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ConfigError("important_parameters 必须是映射")
    normalized: dict[str, tuple[str, ...]] = {}
    for action_name, parameter_names in value.items():
        if not isinstance(action_name, str) or not action_name.strip():
            raise ConfigError("important_parameters 的动作名必须是非空字符串")
        if isinstance(parameter_names, (str, bytes)) or not isinstance(
            parameter_names,
            (list, tuple),
        ):
            raise ConfigError(f"{action_name} 的重要参数必须是序列")
        names: list[str] = []
        for parameter_name in parameter_names:
            if not isinstance(parameter_name, str) or not parameter_name:
                raise ConfigError(f"{action_name} 的重要参数名必须是非空字符串")
            names.append(parameter_name)
        if len(names) != len(set(names)):
            raise ConfigError(f"{action_name} 的重要参数不能重复")
        normalized[action_name] = tuple(names)
    return normalized


__all__ = [
    "BufferOverflowPolicy",
    "BufferUpdateConfig",
    "CacheActionCandidatePolicy",
    "CacheBootConfig",
    "CacheBootMode",
    "CacheColdStartMode",
    "CacheParameterMode",
    "CacheRepresentationConfig",
    "CacheSharingMode",
    "COAPConfig",
    "ConfigError",
    "DEFAULT_ACTION_READY_KEYWORD",
    "DEFAULT_IMPORTANT_PARAMETERS",
    "ReasonSamplingPolicy",
    "RetrievalConfig",
    "ScoreConfig",
    "ScoreStrategy",
    "TieBreakPolicy",
    "default_important_parameters",
]
