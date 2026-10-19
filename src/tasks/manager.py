"""任务管理器：统一协议、基类、完成判定上下文与真实数据读取。"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import UUID

from src.runtime.liveness import mark_agents_live

from .spec import TaskSpec, TaskSpecError


DEFAULT_AGENT_NAMES: tuple[str, ...] = ("Alice", "Bob", "Carol", "Dave", "Eve")
DEATH_SCORE_THRESHOLD = 1

_INITIAL_EQUIPMENT_SLOTS = {
    "helmet": "armor.head",
    "chestplate": "armor.chest",
    "leggings": "armor.legs",
    "boots": "armor.feet",
    "elytra": "armor.chest",
    "turtle_helmet": "armor.head",
    "shield": "weapon.offhand",
}


class TaskStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass
class TaskOutcome:
    """一次完成判定的结果。"""

    status: TaskStatus
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass
class EvaluatorContext:
    """完成判定的真实数据源（由 runner 注入，管理器不依赖具体实现）。"""

    server: Any
    perception: Callable[[str], dict[str, Any] | None]
    elapsed_seconds: Callable[[], float]
    now: Callable[[], float] = time.time

    def health(self, agent_id: str) -> float | None:
        snapshot = self._snapshot(agent_id)
        if snapshot is None:
            return None
        value = snapshot.get("self", {}).get("health")
        if value is None:
            value = snapshot.get("health")
        return float(value) if value is not None else None

    def inventory(self, agent_id: str) -> dict[str, int] | None:
        snapshot = self._snapshot(agent_id)
        if snapshot is None:
            return None
        value = snapshot.get("self", {}).get("inventory")
        if value is None:
            value = snapshot.get("inventory")
        return _normalize_inventory(value)

    def _snapshot(self, agent_id: str) -> dict[str, Any] | None:
        try:
            snapshot = self.perception(agent_id)
        except Exception:  # noqa: BLE001
            return None
        return snapshot if isinstance(snapshot, dict) else None


def snapshot_agent_state(
    snapshot: Mapping[str, Any] | None,
) -> tuple[bool | None, float | None, str | None]:
    """Return ``(dead, health, game_mode)`` from one perception snapshot."""
    if not isinstance(snapshot, Mapping):
        return None, None, None
    observed = False
    health: float | None = None
    game_mode: str | None = None
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, Mapping):
            continue
        raw_mode = candidate.get("gameMode")
        if raw_mode is None:
            raw_mode = candidate.get("gamemode")
        if isinstance(raw_mode, (int, float)) and int(raw_mode) == 3:
            game_mode = "spectator"
            observed = True
        elif isinstance(raw_mode, str) and raw_mode.strip():
            game_mode = raw_mode.strip().lower()
            observed = True
        raw_health = candidate.get("health")
        if isinstance(raw_health, (int, float)):
            health = float(raw_health)
            observed = True
        if game_mode == "spectator" or (health is not None and health <= 0):
            return True, health, game_mode
    return (False if observed else None), health, game_mode


def snapshot_game_mode(snapshot: Mapping[str, Any] | None) -> str | None:
    """Return the game mode reported by an agent snapshot."""
    if not isinstance(snapshot, Mapping):
        return None
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, Mapping):
            continue
        raw_mode = candidate.get("gameMode")
        if raw_mode is None:
            raw_mode = candidate.get("gamemode")
        if isinstance(raw_mode, (int, float)) and int(raw_mode) == 3:
            return "spectator"
        if isinstance(raw_mode, str) and raw_mode.strip():
            return raw_mode.strip().lower()
    return None


def death_score_indicates_elimination(score: int | None) -> bool:
    """Return whether the death counter records at least one death."""
    return score is not None and score >= DEATH_SCORE_THRESHOLD


def send_priority_command(server: Any, command: str) -> Any:
    """Send a high-priority command while keeping lightweight test servers compatible."""
    sender = getattr(server, "send_priority_command", None)
    if callable(sender):
        return sender(command)
    return server.send_command(command)


def spectator_score_selector(agent_name: str, death_objective: str) -> str:
    """Build the method-two selector for one task Agent."""
    return (
        f"gamemode spectator @a[name={agent_name},"
        f"scores={{{death_objective}={DEATH_SCORE_THRESHOLD}..}}]"
    )


def spectator_command_verified(
    response: Any,
    snapshot: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    """Validate method-two spectator setup."""
    game_mode = snapshot_game_mode(snapshot)
    if game_mode == "spectator":
        return True, None
    text = str(response or "").strip()
    lowered = text.lower()
    failure_markers = (
        "unknown or incomplete",
        "incorrect argument",
        "no player was found",
        "no entity was found",
        "does not exist",
        "cannot",
        "usage:",
        "failed",
        "失败",
        "不存在",
    )
    if not text or any(marker in lowered for marker in failure_markers):
        return False, text or "命令没有返回成功响应"
    if "spectator" in lowered or "旁观者" in text:
        return True, None
    if game_mode is not None:
        return False, f"验证到 gameMode={game_mode}"
    return True, None


@runtime_checkable
class TaskManager(Protocol):
    """任务管理器统一协议（方法无关，runner 与任务管理器据此自由搭配）。"""

    def configure(self, spec: TaskSpec) -> None:
        """接收任务规格（由初始化文件解析而来）。"""

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        """环境装配：基线设置、初始状态、生成实体。"""

    def spawn_initial_entities(self, server: Any) -> None:
        """统一准备期结束后召唤初始实体（任务计时开始前）。"""

    def start(self) -> None:
        """环境装配完成后启动任务计时及动态行为。"""

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        """基于真实环境数据判定任务是否结束。"""

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        """指标接口（本轮仅占位，后续开发具体指标）。"""

    def system_message(self, context: EvaluatorContext) -> str | None:
        """返回需要广播并写入聊天记录的任务系统消息；无消息返回 None。"""

    def teardown(self) -> None:
        """释放任务相关资源。"""


class BaseTaskManager:
    """任务管理器基类：通用环境装配、完成判定与真实数据读取。"""

    max_elimination_attempts = 3

    def __init__(self) -> None:
        self.spec: TaskSpec | None = None
        self.started_at: float | None = None
        self.boss_uuids: list[str] = []
        self.boss_max_health: float | None = None
        self._prepared = False
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def configure(self, spec: TaskSpec) -> None:
        self.spec = spec
        self.started_at = None
        self.boss_uuids = []
        self.boss_max_health = None
        self._prepared = False
        self._started = False
        self._validate_spec(spec)

    def _validate_spec(self, spec: TaskSpec) -> None:
        """category 特有校验，子类覆盖。"""

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        self.started_at = time.time()
        names = self._agent_names(agents)
        commands: list[str] = []
        commands.extend(self._baseline_commands())
        commands.extend(self._initial_state_commands(names))
        self._apply_commands(server, commands)
        mark_agents_live(agents, True)
        self._prepared = True

    def spawn_initial_entities(self, server: Any) -> None:
        """召唤 spec.spawns 中的初始实体并捕获 Boss 引用。

        独立于 prepare：由会话在统一准备期结束后、start 之前调用，
        保证初始怪物出现在准备期之后。
        """
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        commands = self._spawn_commands()
        self._apply_commands(server, commands)
        self._capture_boss(server)

    def start(self) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if not self._prepared:
            raise TaskSpecError("任务管理器尚未 prepare")
        if self._started:
            raise TaskSpecError("任务管理器已经 start")
        self.started_at = time.time()
        self._started = True

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        elapsed = context.elapsed_seconds()
        timeout = float(self.spec.completion["timeout_seconds"])
        success = self.spec.completion.get("success") or {}
        stype = success.get("type")

        if stype == "survive_until":
            duration = float(success.get("duration_seconds", 0))
            if elapsed >= duration:
                return TaskOutcome(TaskStatus.SUCCESS, reason="survive_until")
        elif stype == "item_counts":
            totals = self._inventory_totals(context)
            required = success.get("items") or {}
            if all(
                int(totals.get(item, 0)) >= int(count)
                for item, count in required.items()
            ):
                return TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="item_counts",
                    detail={"inventory": totals},
                )
        elif stype == "team_eliminated":
            # 预留：PVP 分队判定，后续实现
            return TaskOutcome(
                TaskStatus.RUNNING,
                detail={"note": "team_eliminated 待后续实现"},
            )

        failure = self.spec.completion.get("failure") or {}
        if failure.get("type") == "all_agents_dead" and self._all_agents_dead(context):
            return TaskOutcome(TaskStatus.FAILURE, reason="all_agents_dead")
        if elapsed >= timeout:
            return TaskOutcome(TaskStatus.TIMEOUT, reason="timeout")
        return TaskOutcome(TaskStatus.RUNNING)

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        """指标接口占位：本轮不定义具体指标，后续开发。"""
        return {}

    def system_message(self, context: EvaluatorContext) -> str | None:
        """任务系统消息接口占位：子类按需覆盖。"""
        return None

    def teardown(self) -> None:
        self.spec = None
        self.started_at = None
        self.boss_uuids = []
        self.boss_max_health = None
        self._prepared = False
        self._started = False

    # ------------------------------------------------------------------
    # 环境装配（结构化字段 -> RCON 命令 + init_commands 兜底）
    # ------------------------------------------------------------------

    def _baseline_commands(self) -> list[str]:
        env = self.spec.environment if self.spec else {}
        commands: list[str] = []
        if env.get("time") is not None:
            commands.append(f"time set {env['time']}")
        if env.get("weather") is not None:
            commands.append(f"weather {env['weather']}")
        if env.get("difficulty") is not None:
            commands.append(f"difficulty {env['difficulty']}")
        if env.get("gamemode") is not None:
            for name in self._agent_names([]):
                commands.append(f"gamemode {env['gamemode']} {name}")
        for key, value in (env.get("gamerules") or {}).items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            commands.append(f"gamerule {key} {text}")
        spawn = env.get("world_spawn")
        if spawn:
            commands.append(f"setworldspawn {spawn['x']} {spawn['y']} {spawn['z']}")
        commands.extend(env.get("init_commands") or [])
        return commands

    def _initial_state_commands(self, names: list[str]) -> list[str]:
        state = self.spec.initial_state if self.spec else {}
        commands: list[str] = []
        per_agent = state.get("per_agent") or {}
        for name in names:
            commands.extend(self._initial_block_commands(name, per_agent))
        for override in state.get("by_agent") or []:
            commands.extend(
                self._initial_block_commands(str(override["name"]), override)
            )
        return commands

    def _initial_block_commands(
        self,
        name: str,
        block: dict[str, Any],
    ) -> list[str]:
        """初始状态 -> RCON 命令。

        inventory 通过 give 发放，equipment 直接写入玩家装备槽，保证任务
        开始时装备已经穿戴，而不是只存在于背包中。
        """
        commands: list[str] = []
        for item, count in (block.get("inventory") or {}).items():
            commands.append(f"give {name} {item} {count}")
        for item in block.get("equipment") or []:
            commands.append(
                f"item replace entity {name} {_initial_equipment_slot(item)} "
                f"with {item} 1"
            )
        position = block.get("position")
        if position:
            commands.append(f"tp {name} {position['x']} {position['y']} {position['z']}")
        return commands

    def _spawn_commands(self) -> list[str]:
        commands: list[str] = []
        for spawn in self.spec.spawns if self.spec else []:
            position = spawn["position"]
            nbt = ""
            if self.spec is not None and spawn.get("type") == self._boss_type():
                boss_nbt = (self.spec.params or {}).get("boss_nbt")
                initial_health = (self.spec.params or {}).get("boss_initial_health")
                if isinstance(boss_nbt, str) and boss_nbt.strip():
                    nbt = f" {boss_nbt.strip()}"
                elif initial_health is not None:
                    nbt = f" {{Health: {float(initial_health)}f}}"
            for _ in range(int(spawn.get("count", 1))):
                commands.append(
                    f"summon {spawn['type']} {position['x']} {position['y']} {position['z']}{nbt}"
                )
        return commands

    def _apply_commands(self, server: Any, commands: list[str]) -> list[str]:
        responses: list[str] = []
        for command in commands:
            responses.append(self._rcon(server, command))
        return responses

    def _rcon(self, server: Any, command: str) -> str:
        return server.send_command(command)

    @staticmethod
    def _cooperative_team_commands(team_id: str, names: list[str]) -> list[str]:
        commands = [
            f"team remove {team_id}",
            f"team add {team_id}",
            f"team modify {team_id} friendlyFire false",
        ]
        commands.extend(f"team join {team_id} {name}" for name in names)
        return commands

    # ------------------------------------------------------------------
    # 真实数据读取（BOSS 血量）
    # ------------------------------------------------------------------

    def _boss_type(self) -> str:
        if self.spec is None:
            return ""
        return str((self.spec.params or {}).get("boss_type") or "")

    def _capture_boss(self, server: Any) -> None:
        boss_type = self._boss_type()
        if not boss_type or not any(
            spawn.get("type") == boss_type for spawn in (self.spec.spawns if self.spec else [])
        ):
            return
        response = self._rcon(
            server,
            f"data get entity @e[type={boss_type},limit=1] UUID",
        )
        uuid_text = _parse_uuid(response)
        if uuid_text:
            self.boss_uuids = [uuid_text]
            health_response = self._rcon(
                server,
                f"data get entity {uuid_text} Health",
            )
            health = _parse_health(health_response)
            max_health = self._query_boss_max_health(server, uuid_text)
            if max_health is not None or health is not None:
                self.boss_max_health = max_health if max_health is not None else health

    def _query_boss_max_health(
        self,
        server: Any,
        target: str | None = None,
    ) -> float | None:
        if target is None and not self._boss_type():
            return None
        entity = target or (
            self.boss_uuids[0]
            if self.boss_uuids
            else f"@e[type={self._boss_type()},limit=1]"
        )
        try:
            response = self._rcon(
                server,
                f"attribute {entity} minecraft:generic.max_health get",
            )
        except Exception:  # noqa: BLE001
            return None
        return _parse_trailing_number(response)

    def _query_boss_health(self, server: Any) -> float | None:
        if not self.boss_uuids:
            boss_type = self._boss_type()
            if not boss_type:
                return None
            try:
                response = self._rcon(
                    server,
                    f"data get entity @e[type={boss_type},limit=1] Health",
                )
            except Exception:  # noqa: BLE001
                return None
            return _parse_health(response)
        healths: list[float] = []
        for uuid_text in self.boss_uuids:
            try:
                response = self._rcon(
                    server,
                    f"data get entity {uuid_text} Health",
                )
            except Exception:  # noqa: BLE001
                return None
            healths.append(_parse_health(response) if response else 0.0)
        if not healths:
            return None
        return max(healths)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _agent_names(self, agents: list[Any]) -> list[str]:
        if agents:
            return [
                name if isinstance(name, str) else getattr(name, "username", str(name))
                for name in agents
            ]
        if self.spec is None:
            return []
        names = self.spec.agents.get("names") or []
        if names:
            return list(names)
        count = int(self.spec.agents["count"])
        if count <= len(DEFAULT_AGENT_NAMES):
            return list(DEFAULT_AGENT_NAMES[:count])
        return [f"agent-{index}" for index in range(count)]

    def _all_agents_dead(self, context: EvaluatorContext) -> bool:
        healths = [
            context.health(name) for name in self._agent_names([])
        ]
        present = [health for health in healths if health is not None]
        return bool(present) and all(health <= 0 for health in present)

    def _inventory_totals(self, context: EvaluatorContext) -> dict[str, int]:
        totals: dict[str, int] = {}
        for name in self._agent_names([]):
            inventory = context.inventory(name)
            if not inventory:
                continue
            for item, count in inventory.items():
                totals[item] = totals.get(item, 0) + int(count)
        return totals


def _initial_equipment_slot(item: str) -> str:
    """Return the Minecraft entity slot for an initial equipment item."""
    item_id = item.strip().lower().removeprefix("minecraft:").split("{", 1)[0]
    for suffix, slot in _INITIAL_EQUIPMENT_SLOTS.items():
        if item_id == suffix or item_id.endswith(f"_{suffix}"):
            return slot
    return "weapon.mainhand"


def _normalize_inventory(value: Any) -> dict[str, int]:
    """把感知背包归一化为 {item: count}。

    支持两种格式:
      - dict: {"bow": 1, ...}（测试/旧格式）
      - list: [{name: "bow", count: 1}, ...]（bridge /perception/raw 真实格式）
    """
    if not isinstance(value, dict):
        if isinstance(value, list):
            normalized: dict[str, int] = {}
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("type")
                count = item.get("count")
                if name and isinstance(count, (int, float)):
                    normalized[str(name)] = (
                        normalized.get(str(name), 0) + int(count)
                    )
            return normalized
        return {}
    normalized = {}
    for item, count in value.items():
        if isinstance(count, (int, float)):
            normalized[str(item)] = int(count)
    return normalized


def _parse_uuid(response: Any) -> str | None:
    if response is None:
        return None
    match = re.search(
        r"\[I;\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]",
        str(response),
    )
    if not match:
        return None
    parts = [int(part) & 0xFFFFFFFF for part in match.groups()]
    combined = (
        (parts[0] << 96) | (parts[1] << 64) | (parts[2] << 32) | parts[3]
    )
    return str(UUID(int=combined))


def _parse_health(response: Any) -> float | None:
    if response is None:
        return None
    text = str(response)
    if "No entity was found" in text:
        return 0.0
    match = re.search(r"entity data:\s*([-+]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def _parse_trailing_number(response: Any) -> float | None:
    if response is None:
        return None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)(?:[fFdD])?\s*$", str(response))
    return float(match.group(1)) if match else None
