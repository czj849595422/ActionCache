"""任务规格：TaskSpec 数据模型、YAML 解析与基础校验。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import threading
from typing import Any

import yaml

from src.perception.constants import DEFAULT_OBSERVATION_RANGE

SUCCESS_TYPES = {
    "all_personal_chests_satisfied",
    "all_task_monsters_eliminated",
    "boss_health_below",
    "item_counts",
    "item_value",
    "team_eliminated",
    "survive_until",
}
FAILURE_TYPES = {"all_agents_dead"}

TIMES = {"day", "night", "noon", "midnight"}
WEATHERS = {"clear", "rain", "thunder"}
DIFFICULTIES = {"peaceful", "easy", "normal", "hard"}
GAMEMODES = {"survival", "creative", "adventure", "spectator"}

_TOP_LEVEL_KEYS = {
    "task_id",
    "category",
    "name",
    # Backward-compatible parse allowance. YAML task descriptions are ignored;
    # prompt injection uses task-folder description files instead.
    "description",
    "agents",
    "environment",
    "initial_state",
    "spawns",
    "completion",
    "trials",
    "output",
    "params",
    "metadata",
}
_ENVIRONMENT_KEYS = {
    "world",
    "seed",
    "observation_range",
    "time",
    "weather",
    "difficulty",
    "gamemode",
    "world_spawn",
    "gamerules",
    "init_commands",
}
_COMPLETION_KEYS = {"timeout_seconds", "success", "failure"}
_INITIAL_BLOCK_KEYS = {"inventory", "equipment", "position", "health", "hunger"}
_DESCRIPTION_CACHE_LOCK = threading.RLock()


class TaskSpecError(ValueError):
    """任务规格解析/校验错误。"""


def load_spec(path: str | Path) -> "TaskSpec":
    """从 YAML 文件加载任务规格（解析 + 校验）。"""
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TaskSpecError(f"无法解析任务文件 {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaskSpecError(f"任务文件 {target} 顶层必须是映射")
    spec = TaskSpec.from_dict(raw)
    spec._source_path = target.resolve()
    return spec


@dataclass
class TaskSpec:
    """任务初始化规格（纯数据，方法无关）。"""

    task_id: str
    category: str
    name: str
    description: str = ""
    agents: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    initial_state: dict[str, Any] = field(default_factory=dict)
    spawns: list[dict[str, Any]] = field(default_factory=list)
    completion: dict[str, Any] = field(default_factory=dict)
    trials: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    _source_path: Path | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _description_text_cache: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _description_text_loaded: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        unknown = set(data) - _TOP_LEVEL_KEYS
        if unknown:
            raise TaskSpecError(f"未知顶层字段: {sorted(unknown)}")
        spec = cls(
            task_id=str(data.get("task_id", "")),
            category=str(data.get("category", "")),
            name=str(data.get("name", "")),
            description="",
            agents=dict(data.get("agents") or {}),
            environment=dict(data.get("environment") or {}),
            initial_state=dict(data.get("initial_state") or {}),
            spawns=list(data.get("spawns") or []),
            completion=dict(data.get("completion") or {}),
            trials=dict(data.get("trials") or {}),
            output=dict(data.get("output") or {}),
            params=dict(data.get("params") or {}),
            metadata=dict(data.get("metadata") or {}),
        )
        validate_spec(spec)
        return spec

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_source_path", None)
        data.pop("_description_text_cache", None)
        data.pop("_description_text_loaded", None)
        data.pop("description", None)
        return data

    @property
    def source_path(self) -> Path | None:
        """加载该规格的 YAML 路径；通过 from_dict 创建时为 None。"""
        return self._source_path

    def resolve_resource(self, value: str | Path) -> Path:
        """相对任务 YAML 解析外部资源，内存规格则相对当前目录。"""
        target = Path(value)
        if target.is_absolute():
            return target
        base = self._source_path.parent if self._source_path else Path.cwd()
        return (base / target).resolve()

    @property
    def description_file(self) -> Path | None:
        """任务文件夹下的描述文本文件。"""
        candidates = self.description_file_candidates()
        for path in candidates:
            if path.is_file():
                return path
        return candidates[0] if candidates else None

    def description_file_candidates(self) -> tuple[Path, ...]:
        """按优先级返回任务描述候选路径。"""
        task_dir = self._description_directory()
        if task_dir is None:
            return ()
        candidates = [
            task_dir / f"{self.task_id}.description.txt",
            task_dir / "description.txt",
        ]
        if self._source_path is not None and self._source_path.parent.name != "config":
            legacy = self._source_path.with_name(
                f"{self._source_path.stem}.description.txt"
            )
            if legacy.parent == task_dir and legacy not in candidates:
                candidates.append(legacy)
        return tuple(candidates)

    def _description_directory(self) -> Path | None:
        if self._source_path is None:
            return None
        source_dir = self._source_path.parent
        if source_dir.name == "config":
            return source_dir.parent
        if source_dir.name == "specs":
            task_dir = source_dir.parent / self.task_id
            if task_dir.is_dir():
                return task_dir
        return source_dir

    def load_description_text(self) -> str | None:
        """读取任务描述文本；同一 TaskSpec 生命周期内只读取一次。"""
        with _DESCRIPTION_CACHE_LOCK:
            if self._description_text_loaded:
                return self._description_text_cache
            result: str | None = None
            for path in self.description_file_candidates():
                if path.is_file():
                    result = path.read_text(encoding="utf-8").strip()
                    break
            self._description_text_cache = result
            self._description_text_loaded = True
            return result

    @property
    def observation_range(self) -> float:
        """Agent 感知范围（米）；任务未说明时默认 20.0。"""
        return float(self.environment.get("observation_range", DEFAULT_OBSERVATION_RANGE))


def validate_spec(spec: TaskSpec) -> None:
    """基础校验（字段/类型/枚举/坐标）；category 特有校验由管理器完成。"""
    if not spec.task_id:
        raise TaskSpecError("task_id 必填")
    if not spec.category:
        raise TaskSpecError("category 必填")
    if not spec.name:
        raise TaskSpecError("name 必填")

    _validate_agents(spec.agents)
    _validate_environment(spec.environment)
    _validate_initial_state(spec.initial_state)
    _validate_spawns(spec.spawns)
    _validate_completion(spec.completion)
    _validate_trials(spec.trials)
    for name, value in (("output", spec.output), ("params", spec.params), ("metadata", spec.metadata)):
        if not isinstance(value, dict):
            raise TaskSpecError(f"{name} 必须是映射")


def _validate_agents(agents: dict[str, Any]) -> None:
    count = agents.get("count")
    if not isinstance(count, int) or count < 1:
        raise TaskSpecError("agents.count 必须是 >=1 的整数")
    names = agents.get("names") or []
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not name for name in names
    ):
        raise TaskSpecError("agents.names 必须是非空字符串列表")
    if names and len(names) != count:
        raise TaskSpecError("agents.names 数量必须等于 agents.count")
    teams = agents.get("teams") or []
    if not isinstance(teams, list) or any(
        not isinstance(team, dict) for team in teams
    ):
        raise TaskSpecError("agents.teams 必须是对象列表")


def _validate_environment(env: dict[str, Any]) -> None:
    unknown = set(env) - _ENVIRONMENT_KEYS
    if unknown:
        raise TaskSpecError(f"environment 存在未知字段: {sorted(unknown)}")
    if "world" in env and env["world"] is not None and not isinstance(env["world"], str):
        raise TaskSpecError("environment.world 必须是字符串")
    if "seed" in env and env["seed"] is not None and not isinstance(env["seed"], int):
        raise TaskSpecError("environment.seed 必须是整数")
    observation_range = env.get("observation_range")
    if observation_range is not None and (
        not isinstance(observation_range, (int, float)) or observation_range <= 0
    ):
        raise TaskSpecError("environment.observation_range 必须是大于 0 的数字")
    time_value = env.get("time")
    if time_value is not None:
        if isinstance(time_value, str):
            if time_value not in TIMES and not time_value.isdigit():
                raise TaskSpecError(f"environment.time 非法: {time_value!r}")
        elif not isinstance(time_value, int):
            raise TaskSpecError(f"environment.time 非法: {time_value!r}")
    weather = env.get("weather")
    if weather is not None and weather not in WEATHERS:
        raise TaskSpecError(f"environment.weather 非法: {weather!r}")
    difficulty = env.get("difficulty")
    if difficulty is not None and difficulty not in DIFFICULTIES:
        raise TaskSpecError(f"environment.difficulty 非法: {difficulty!r}")
    gamemode = env.get("gamemode")
    if gamemode is not None and gamemode not in GAMEMODES:
        raise TaskSpecError(f"environment.gamemode 非法: {gamemode!r}")
    if "world_spawn" in env and env["world_spawn"] is not None:
        _validate_position(env["world_spawn"], "environment.world_spawn")
    gamerules = env.get("gamerules") or {}
    if not isinstance(gamerules, dict):
        raise TaskSpecError("environment.gamerules 必须是映射")
    init_commands = env.get("init_commands") or []
    if not isinstance(init_commands, list) or any(
        not isinstance(cmd, str) for cmd in init_commands
    ):
        raise TaskSpecError("environment.init_commands 必须是字符串列表")


def _validate_initial_state(state: dict[str, Any]) -> None:
    per_agent = state.get("per_agent") or {}
    if not isinstance(per_agent, dict):
        raise TaskSpecError("initial_state.per_agent 必须是映射")
    _validate_initial_block(per_agent, "initial_state.per_agent")
    by_agent = state.get("by_agent") or []
    if not isinstance(by_agent, list):
        raise TaskSpecError("initial_state.by_agent 必须是列表")
    for block in by_agent:
        if not isinstance(block, dict) or not block.get("name"):
            raise TaskSpecError("initial_state.by_agent 元素必须是含 name 的映射")
        _validate_initial_block(block, "initial_state.by_agent[]")


def _validate_initial_block(block: dict[str, Any], label: str) -> None:
    unknown = set(block) - _INITIAL_BLOCK_KEYS - {"name"}
    if unknown:
        raise TaskSpecError(f"{label} 存在未知字段: {sorted(unknown)}")
    inventory = block.get("inventory") or {}
    if not isinstance(inventory, dict) or any(
        not isinstance(item, str) or not isinstance(count, int) or count < 1
        for item, count in inventory.items()
    ):
        raise TaskSpecError(f"{label}.inventory 必须是 {item: count>=1} 映射")
    equipment = block.get("equipment") or []
    if not isinstance(equipment, list) or any(
        not isinstance(item, str) or not item for item in equipment
    ):
        raise TaskSpecError(f"{label}.equipment 必须是非空字符串列表")
    if "position" in block and block["position"] is not None:
        _validate_position(block["position"], f"{label}.position")
    for key in ("health", "hunger"):
        value = block.get(key)
        if value is not None and not isinstance(value, (int, float)):
            raise TaskSpecError(f"{label}.{key} 必须是数字")


def task_description_text(spec: Any, agent_id: str | None = None) -> str:
    """Return the task description plus the optional Trade identity context."""
    load = getattr(spec, "load_description_text", None)
    text = load() if callable(load) else ""
    result = str(text).strip() if text else ""
    if getattr(spec, "category", "") == "trade":
        from .trade.prompt import render_trade_prompt_context

        context = render_trade_prompt_context(spec, agent_id=agent_id)
        if context:
            return "\n\n".join(part for part in (result, context) if part)
    return result


def _validate_spawns(spawns: list[dict[str, Any]]) -> None:
    if not isinstance(spawns, list):
        raise TaskSpecError("spawns 必须是列表")
    for index, spawn in enumerate(spawns):
        if not isinstance(spawn, dict):
            raise TaskSpecError(f"spawns[{index}] 必须是映射")
        unknown = set(spawn) - {"type", "position", "count"}
        if unknown:
            raise TaskSpecError(f"spawns[{index}] 存在未知字段: {sorted(unknown)}")
        if not isinstance(spawn.get("type"), str) or not spawn["type"]:
            raise TaskSpecError(f"spawns[{index}].type 必填")
        _validate_position(spawn.get("position"), f"spawns[{index}].position")
        count = spawn.get("count", 1)
        if not isinstance(count, int) or count < 1:
            raise TaskSpecError(f"spawns[{index}].count 必须是 >=1 的整数")


def _validate_completion(completion: dict[str, Any]) -> None:
    unknown = set(completion) - _COMPLETION_KEYS
    if unknown:
        raise TaskSpecError(f"completion 存在未知字段: {sorted(unknown)}")
    timeout = completion.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise TaskSpecError("completion.timeout_seconds 必须是大于 0 的数字")
    success = completion.get("success")
    if not isinstance(success, dict) or success.get("type") not in SUCCESS_TYPES:
        raise TaskSpecError(
            f"completion.success.type 必须属于 {sorted(SUCCESS_TYPES)}"
        )
    stype = success["type"]
    if stype == "boss_health_below":
        ratio = success.get("ratio")
        if not isinstance(ratio, (int, float)) or not (0 <= ratio <= 1):
            raise TaskSpecError("completion.success.ratio 必须是 [0, 1] 内的数字")
    elif stype == "item_counts":
        items = success.get("items")
        if not isinstance(items, dict) or not items:
            raise TaskSpecError("completion.success.items 必须是 {item: count} 映射")
    elif stype == "item_value":
        target = success.get("target_value")
        values = success.get("item_values")
        if not isinstance(target, (int, float)) or target <= 0:
            raise TaskSpecError("completion.success.target_value 必须大于 0")
        if not isinstance(values, dict) or not values:
            raise TaskSpecError(
                "completion.success.item_values 必须是非空 {item: value} 映射"
            )
        if any(
            not isinstance(item, str)
            or not item
            or not isinstance(value, (int, float))
            or value <= 0
            for item, value in values.items()
        ):
            raise TaskSpecError(
                "completion.success.item_values 的名称必须非空且价值必须大于 0"
            )
    elif stype == "survive_until":
        duration = success.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise TaskSpecError("completion.success.duration_seconds 必须大于 0")
    failure = completion.get("failure") or {}
    if failure:
        if not isinstance(failure, dict) or failure.get("type") not in FAILURE_TYPES:
            raise TaskSpecError(
                f"completion.failure.type 必须属于 {sorted(FAILURE_TYPES)}"
            )


def _validate_trials(trials: dict[str, Any]) -> None:
    if not trials:
        return
    count = trials.get("count", 1)
    if not isinstance(count, int) or count < 1:
        raise TaskSpecError("trials.count 必须是 >=1 的整数")
    seeds = trials.get("seeds") or []
    if not isinstance(seeds, list) or any(
        not isinstance(seed, int) for seed in seeds
    ):
        raise TaskSpecError("trials.seeds 必须是整数列表")


def _validate_position(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise TaskSpecError(f"{label} 必须是 {{x, y, z}} 映射")
    for key in ("x", "y", "z"):
        if key not in value or not isinstance(value[key], (int, float)):
            raise TaskSpecError(f"{label}.{key} 必须是数字")
