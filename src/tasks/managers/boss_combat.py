"""BOSS 战任务管理器（示例类别：完成判定读真实 BOSS 血量）。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import random
import re
import threading
from typing import Any

from src.runtime.liveness import set_agent_live_by_name
from src.tasks.boss_combat.arena import (
    BOSS_ARENA_WALL_COMMANDS,
    is_boss_spawn_inside,
)
from src.tasks.manager import (
    BaseTaskManager,
    EvaluatorContext,
    TaskOutcome,
    TaskStatus,
    death_score_indicates_elimination,
    send_priority_command,
    snapshot_game_mode,
    spectator_score_selector,
    spectator_command_verified,
)
from src.tasks.spec import TaskSpec, TaskSpecError


DEFAULT_HOSTILE_TYPES: tuple[str, ...] = (
    "minecraft:wither",
    "minecraft:zombie",
    "minecraft:skeleton",
    "minecraft:creeper",
    "minecraft:spider",
    "minecraft:cave_spider",
    "minecraft:enderman",
    "minecraft:witch",
    "minecraft:slime",
    "minecraft:magma_cube",
    "minecraft:drowned",
    "minecraft:husk",
    "minecraft:stray",
    "minecraft:pillager",
    "minecraft:vindicator",
    "minecraft:evoker",
    "minecraft:ravager",
    "minecraft:phantom",
    "minecraft:wither_skeleton",
    "minecraft:blaze",
    "minecraft:ghast",
    "minecraft:zombified_piglin",
    "minecraft:piglin",
    "minecraft:piglin_brute",
    "minecraft:hoglin",
    "minecraft:zoglin",
    "minecraft:guardian",
    "minecraft:elder_guardian",
    "minecraft:shulker",
    "minecraft:silverfish",
    "minecraft:endermite",
)

DEFAULT_SUPPLY_DROP_ITEMS: tuple[dict[str, object], ...] = (
    {"item": "minecraft:bow", "amount": 1, "weight": 1.0},
    {"item": "minecraft:arrow", "amount": 32, "weight": 3.0},
    {"item": "minecraft:golden_apple", "amount": 1, "weight": 2.0},
    {
        "item": "minecraft:potion",
        "amount": 1,
        "profile": "healing",
        "weight": 3.0,
    },
)
_COOPERATIVE_TEAM = "ac_boss_combat"


@dataclass(frozen=True)
class SupplyDropItem:
    item: str
    amount: int
    weight: float = 1.0
    profile: str = "default"
    tag: str | None = None


class BossCombatManager(BaseTaskManager):
    """boss_combat 类别管理器。

    - success.type 仅支持 boss_health_below（BOSS 血量比例 ≤ ratio 判成功）；
    - 血量通过 RCON 查询已跟踪 UUID 的 BOSS 实体（真实环境数据），
      不使用聊天/SystemInfo 等通知作为判定依据；
    - 死亡 Agent 自动切换为 spectator，并按当前背包/装备掉落物品。
    """

    system_message_interval_seconds = 20.0
    max_elimination_attempts = 3

    def __init__(self) -> None:
        super().__init__()
        self._last_system_message_at: float | None = None
        self._server: Any = None
        self._agents: list[Any] = []
        self._runtime_agent_names: list[str] = []
        self._supply_drop_items: tuple[SupplyDropItem, ...] = ()
        self._supply_stop_event = threading.Event()
        self._supply_thread: threading.Thread | None = None
        self._supply_error: Exception | None = None
        self._supply_drops_sent = 0
        self._last_supply_drop: dict[str, object] | None = None
        self._eliminated_agents: set[str] = set()
        self._pending_eliminations: set[str] = set()
        self._elimination_attempts: dict[str, int] = {}
        self._elimination_error: dict[str, str] | None = None
        self._last_query_note: str | None = None
        self._death_drop_tag = "ac_boss_death"
        self._death_objective = "ac_boss_death_count"
        self._state_lock = threading.Lock()
        self._rng = random.Random()

    def configure(self, spec: TaskSpec) -> None:
        if self._supply_thread is not None and self._supply_thread.is_alive():
            raise TaskSpecError("Boss 补给掉落线程仍在运行，请先 teardown")
        self._reset_runtime_state()
        super().configure(spec)
        self._last_system_message_at = None

    def _validate_spec(self, spec: TaskSpec) -> None:
        boss_type = str((spec.params or {}).get("boss_type") or "")
        if not boss_type:
            raise TaskSpecError("params.boss_type 必填（如 wither/ender_dragon/elder_guardian）")
        success = spec.completion.get("success") or {}
        if success.get("type") != "boss_health_below":
            raise TaskSpecError(
                "boss_combat 仅支持 completion.success.type=boss_health_below，"
                f"当前: {success.get('type')}"
            )
        for index, spawn in enumerate(spec.spawns):
            if spawn.get("type") != boss_type:
                continue
            position = spawn.get("position")
            if not isinstance(position, Mapping) or not is_boss_spawn_inside(position):
                raise TaskSpecError(
                    f"spawns[{index}] 的 Boss 出生点必须位于围墙内侧: "
                    "x/z 为 [-49, 49]，y 为 [1, 29]"
                )
        params = spec.params or {}
        drop_interval = float(params.get("supply_drop_interval_seconds", 5.0))
        if drop_interval <= 0:
            raise TaskSpecError("params.supply_drop_interval_seconds 必须大于 0")
        min_distance = float(params.get("supply_drop_min_distance", 10.0))
        max_distance = float(params.get("supply_drop_max_distance", 20.0))
        if min_distance < 0 or max_distance < min_distance:
            raise TaskSpecError(
                "params.supply_drop_min_distance/max_distance 必须满足 0 <= min <= max"
            )
        self._supply_drop_items = _parse_supply_drop_items(
            params.get("supply_drop_items", DEFAULT_SUPPLY_DROP_ITEMS)
        )

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        self._server = server
        self._agents = list(agents)
        self._runtime_agent_names = self._agent_names(agents)
        super().prepare(server, agents, memory)
        self._apply_commands(server, list(BOSS_ARENA_WALL_COMMANDS))
        self._apply_commands(
            server,
            self._cooperative_team_commands(
                _COOPERATIVE_TEAM,
                self._runtime_agent_names,
            ),
        )
        self._prepare_death_scoreboard(server, self._runtime_agent_names)

    def spawn_initial_entities(self, server: Any) -> None:
        self._clear_hostiles_before_boss(server)
        super().spawn_initial_entities(server)

    def start(self) -> None:
        super().start()
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        if self._supply_drops_enabled():
            self._supply_stop_event.clear()
            self._supply_thread = threading.Thread(
                target=self._run_supply_drop_loop,
                name=f"boss-supply-{self.spec.task_id if self.spec else 'task'}",
                daemon=True,
            )
            self._supply_thread.start()

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        with self._state_lock:
            supply_error = self._supply_error
        if supply_error is not None:
            self._supply_stop_event.set()
            return TaskOutcome(
                TaskStatus.FAILURE,
                reason="supply_drop_failed",
                detail={"error": str(supply_error)},
            )
        self._last_query_note = None
        self._update_agent_eliminations(context)
        with self._state_lock:
            elimination_error = dict(self._elimination_error) if self._elimination_error else None
        if elimination_error is not None:
            self._supply_stop_event.set()
            return TaskOutcome(
                TaskStatus.FAILURE,
                reason="agent_elimination_failed",
                detail=elimination_error,
            )
        if self._all_agents_dead(context):
            self._supply_stop_event.set()
            return TaskOutcome(TaskStatus.FAILURE, reason="all_agents_dead")
        success = self.spec.completion.get("success") or {}
        if success.get("type") == "boss_health_below":
            ratio = float(success.get("ratio", 0.0))
            current = self._query_boss_health(context.server)
            if current is None:
                outcome = super().completion_check(context)
                if outcome.status == TaskStatus.RUNNING:
                    outcome.detail["boss_health"] = None
                    outcome.detail["note"] = "BOSS 血量查询失败"
                return outcome
            max_health = self.boss_max_health or current
            if max_health > 0 and current / max_health <= ratio:
                self._supply_stop_event.set()
                return TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="boss_health_below",
                    detail={"health": current, "max": max_health},
                )
        outcome = super().completion_check(context)
        if outcome.status != TaskStatus.RUNNING:
            self._supply_stop_event.set()
        return outcome

    def metrics(self, context: EvaluatorContext) -> dict[str, object]:
        """Return a stable Boss progress overlay for task perception and runners."""
        if self.spec is None:
            return {}
        current = self._query_boss_health(context.server)
        position = self._query_boss_position(context.server)
        maximum = self.boss_max_health
        ratio = None
        if current is not None and maximum is not None and maximum > 0:
            ratio = current / maximum
        alive_agents: list[str] = []
        names = self._active_agent_names()
        for name in names:
            dead = self._agent_is_dead(context, name)
            if dead is False:
                alive_agents.append(name)
        elapsed = max(0.0, float(context.elapsed_seconds()))
        timeout = float(self.spec.completion["timeout_seconds"])
        with self._state_lock:
            supply_error = self._supply_error
            supply_drops_sent = self._supply_drops_sent
            last_supply_drop = (
                dict(self._last_supply_drop) if self._last_supply_drop else None
            )
        result: dict[str, object] = {
            "boss_type": self._boss_type(),
            "boss_uuid": self.boss_uuids[0] if self.boss_uuids else None,
            "boss_entity_id": self._query_boss_entity_id(context),
            "boss_position": (
                {"x": position[0], "y": position[1], "z": position[2]}
                if position is not None
                else None
            ),
            "current_health": current,
            "max_health": maximum,
            "current_ratio": ratio,
            "target_ratio": float(self.spec.completion["success"]["ratio"]),
            "alive_agents": alive_agents,
            "total_agents": len(names),
            "elapsed_seconds": elapsed,
            "remaining_seconds": max(0.0, timeout - elapsed),
            "query_error": "BOSS 血量查询失败" if current is None else None,
            "supply_drops_sent": supply_drops_sent,
            "last_supply_drop": last_supply_drop,
            "supply_drop_error": str(supply_error) if supply_error else None,
            "eliminated_agents": sorted(self._eliminated_agents),
            "pending_eliminations": sorted(self._pending_eliminations),
            "elimination_error": (
                dict(self._elimination_error) if self._elimination_error else None
            ),
        }
        if self._last_query_note:
            result["query_note"] = self._last_query_note
        return result

    def system_message(self, context: EvaluatorContext) -> str | None:
        if self.spec is None:
            return None
        now = float(context.now())
        if (
            self._last_system_message_at is not None
            and now - self._last_system_message_at < self.system_message_interval_seconds
        ):
            return None
        health = self._query_boss_health(context.server)
        position = self._query_boss_position(context.server)
        if health is None or position is None:
            return None
        self._last_system_message_at = now
        return (
            f"Boss {self._boss_display_name()}："
            f"位于{_format_position(position)}； 血量{_format_number(health)}。"
        )

    def teardown(self) -> None:
        server = self._server
        self._stop_supply_drop_thread()
        if server is not None and self.spec is not None:
            supply_tag = _supply_entity_tag(self.spec)
            try:
                server.send_command(f"kill @e[type=minecraft:item,tag={supply_tag}]")
            except Exception:  # noqa: BLE001
                pass
            try:
                server.send_command(
                    f"kill @e[type=minecraft:item,tag={self._death_drop_tag}]"
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                server.send_command(f"scoreboard objectives remove {self._death_objective}")
            except Exception:  # noqa: BLE001
                pass
            try:
                server.send_command(f"team remove {_COOPERATIVE_TEAM}")
            except Exception:  # noqa: BLE001
                pass
        self._reset_runtime_state()
        super().teardown()

    def _boss_display_name(self) -> str:
        if self.spec is None:
            return self._boss_type() or "unknown"
        params = self.spec.params or {}
        name = params.get("boss_name") or params.get("boss_type") or self._boss_type()
        return str(name)

    def _query_boss_position(self, server: Any) -> tuple[float, float, float] | None:
        targets = list(self.boss_uuids)
        if not targets:
            boss_type = self._boss_type()
            if not boss_type:
                return None
            targets = [f"@e[type={boss_type},limit=1]"]
        for target in targets:
            try:
                response = self._rcon(server, f"data get entity {target} Pos")
            except Exception:  # noqa: BLE001
                return None
            position = _parse_position(response)
            if position is not None:
                return position
        return None

    def _query_boss_entity_id(self, context: EvaluatorContext) -> int | None:
        """Read the short Mineflayer id when any agent currently sees the Boss."""
        boss_uuid = self.boss_uuids[0] if self.boss_uuids else None
        boss_type = self._boss_type().split(":", 1)[-1].lower()
        if not boss_type:
            return None
        for name in self._active_agent_names():
            snapshot = _context_snapshot(context, name)
            if not isinstance(snapshot, dict):
                continue
            for entity in snapshot.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                entity_uuid = entity.get("uuid")
                entity_name = str(
                    entity.get("name")
                    or entity.get("mobType")
                    or entity.get("type")
                    or ""
                ).split(":", 1)[-1].lower()
                if boss_uuid and entity_uuid is not None:
                    if str(entity_uuid) != str(boss_uuid):
                        continue
                elif entity_name != boss_type:
                    continue
                entity_id = entity.get("id")
                if entity_id is None:
                    entity_id = entity.get("entityId")
                if isinstance(entity_id, bool):
                    continue
                if isinstance(entity_id, int):
                    return entity_id
                if isinstance(entity_id, str) and entity_id.strip().isdigit():
                    return int(entity_id.strip())
        return None

    def _active_agent_names(self) -> list[str]:
        return list(self._runtime_agent_names or self._agent_names([]))

    def _update_agent_eliminations(self, context: EvaluatorContext) -> None:
        server = self._server or context.server
        if server is None:
            return
        for name in self._active_agent_names():
            with self._state_lock:
                pending = name in self._pending_eliminations
            dead = True if pending else self._agent_is_dead(context, name)
            if dead is not True:
                continue
            with self._state_lock:
                if name in self._eliminated_agents:
                    continue
                self._pending_eliminations.add(name)
            set_agent_live_by_name(self._agents, name, False)
            success, error = self._eliminate_agent(context, name)
            if success:
                with self._state_lock:
                    self._pending_eliminations.discard(name)
                    self._eliminated_agents.add(name)
                    self._elimination_attempts.pop(name, None)
                continue
            with self._state_lock:
                attempts = self._elimination_attempts.get(name, 0) + 1
                self._elimination_attempts[name] = attempts
                if attempts >= self.max_elimination_attempts:
                    self._elimination_error = {
                        "agent": name,
                        "error": error or "无法确认旁观者模式",
                        "attempts": str(attempts),
                    }

    def _all_agents_dead(self, context: EvaluatorContext) -> bool:
        del context
        names = self._active_agent_names()
        with self._state_lock:
            return bool(names) and all(name in self._eliminated_agents for name in names)

    def _agent_is_dead(
        self,
        context: EvaluatorContext,
        name: str,
    ) -> bool | None:
        with self._state_lock:
            if name in self._eliminated_agents:
                return True
        server = self._server or context.server
        score = (
            self._query_score(
                server,
                name,
                self._death_objective,
                priority=True,
            )
            if server is not None
            else None
        )
        snapshot = _context_snapshot(context, name)
        game_mode = snapshot_game_mode(snapshot)
        dead = game_mode == "spectator" if game_mode is not None else None
        if score is None:
            self._last_query_note = f"无法读取 {name} 死亡计分"
        if snapshot is None:
            self._last_query_note = (
                f"无法读取 {name} 感知状态"
                if not self._last_query_note
                else f"{self._last_query_note}；无法读取 {name} 感知状态"
            )
        if score is None and dead is None:
            return None
        if death_score_indicates_elimination(score):
            return True
        return dead

    def _prepare_death_scoreboard(self, server: Any, names: list[str]) -> None:
        commands = [
            f"scoreboard objectives remove {self._death_objective}",
            f"scoreboard objectives add {self._death_objective} deathCount",
        ]
        commands.extend(
            f"scoreboard players set {name} {self._death_objective} 0"
            for name in names
        )
        for command in commands:
            try:
                server.send_command(command)
            except Exception:  # noqa: BLE001
                pass

    def _query_score(
        self,
        server: Any,
        holder: str,
        objective: str,
        *,
        priority: bool = False,
    ) -> int | None:
        try:
            command = f"scoreboard players get {holder} {objective}"
            response = (
                send_priority_command(server, command)
                if priority
                else server.send_command(command)
            )
        except Exception:  # noqa: BLE001
            return None
        text = str(response or "")
        if "none is set" in text or "No score" in text:
            return 0
        match = _SCORE_RE.search(text)
        return int(match.group(1)) if match else None

    def _eliminate_agent(
        self,
        context: EvaluatorContext,
        name: str,
    ) -> tuple[bool, str | None]:
        server = self._server or context.server
        if server is None:
            return False, "任务服务器不可用"
        try:
            response = send_priority_command(
                server,
                spectator_score_selector(name, self._death_objective)
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        verified, error = spectator_command_verified(
            response,
            _context_snapshot(context, name),
        )
        if not verified:
            return False, error
        position = self._agent_position(context, name)
        if position is not None:
            for command in self._death_loot_commands(context, name, position):
                try:
                    server.send_command(command)
                except Exception as exc:  # noqa: BLE001
                    self._last_query_note = f"死亡掉落命令失败 [{name}]: {exc}"
        try:
            server.send_command(f"clear {name}")
        except Exception as exc:  # noqa: BLE001
            self._last_query_note = f"清理死亡 Agent 失败 [{name}]: {exc}"
        return True, None

    def _death_loot_commands(
        self,
        context: EvaluatorContext,
        name: str,
        position: tuple[float, float, float],
    ) -> list[str]:
        items = _collect_death_loot(context, name)
        if not items:
            return []
        x, y, z = (_format_number(value) for value in position)
        commands: list[str] = []
        for index, item in enumerate(items, start=1):
            for count in _split_stack_count(item["count"]):
                tag = _snbt(item["nbt"]) if item.get("nbt") else None
                item_nbt = (
                    f'Item:{{id:"{item["id"]}",Count:{count}b'
                    + (f",tag:{tag}" if tag else "")
                    + "}"
                )
                nbt = (
                    f'{{Tags:["{self._death_drop_tag}","ac_boss_death_{name}_{index}"],'
                    f"Age:-32768s,PickupDelay:0s,Motion:[0.0d,0.0d,0.0d],{item_nbt}}}"
                )
                commands.append(f"summon minecraft:item {x} {y} {z} {nbt}")
        return commands

    def _agent_position(
        self,
        context: EvaluatorContext,
        name: str,
    ) -> tuple[float, float, float] | None:
        snapshot = _context_snapshot(context, name)
        if isinstance(snapshot, dict):
            raw_self = snapshot.get("self")
            if isinstance(raw_self, dict):
                position = _position_tuple(raw_self.get("position"))
                if position is not None:
                    return position
            position = _position_tuple(snapshot.get("position"))
            if position is not None:
                return position
        server = self._server or context.server
        if server is None:
            return None
        try:
            response = self._rcon(server, f"data get entity {name} Pos")
        except Exception:  # noqa: BLE001
            return None
        return _parse_position(response)

    def _clear_hostiles_before_boss(self, server: Any) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        params = self.spec.params or {}
        if params.get("clear_hostiles_before_boss", True) is False:
            return
        hostile_types = params.get("clear_hostile_types")
        if hostile_types is None:
            hostile_types = DEFAULT_HOSTILE_TYPES
        if not isinstance(hostile_types, Sequence) or isinstance(hostile_types, str):
            raise TaskSpecError("params.clear_hostile_types 必须是字符串列表")
        seen: set[str] = set()
        for raw_type in hostile_types:
            if not isinstance(raw_type, str) or not raw_type:
                raise TaskSpecError("params.clear_hostile_types 必须是字符串列表")
            entity_type = _namespaced(raw_type)
            if entity_type in seen:
                continue
            seen.add(entity_type)
            try:
                server.send_command(f"kill @e[type={entity_type}]")
            except Exception:  # noqa: BLE001
                pass

    def _supply_drops_enabled(self) -> bool:
        if self.spec is None:
            return False
        return (self.spec.params or {}).get("supply_drops_enabled", True) is not False

    def _run_supply_drop_loop(self) -> None:
        if self.spec is None:
            return
        interval = float((self.spec.params or {}).get("supply_drop_interval_seconds", 5.0))
        while not self._supply_stop_event.wait(interval):
            try:
                self._drop_supply_once()
            except Exception as exc:  # noqa: BLE001
                if not self._supply_stop_event.is_set():
                    with self._state_lock:
                        self._supply_error = exc
                self._supply_stop_event.set()
                break

    def _drop_supply_once(self) -> bool:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        candidates = self._alive_agent_positions(self._server)
        if not candidates:
            return False
        agent_name, position = self._rng.choice(candidates)
        item = _weighted_choice(self._rng, self._supply_drop_items)
        drop_position = self._random_drop_position(position)
        command = _supply_summon_command(
            item,
            drop_position,
            supply_tag=_supply_entity_tag(self.spec),
        )
        response = self._server.send_command(command)
        with self._state_lock:
            self._supply_drops_sent += 1
            self._last_supply_drop = {
                "agent": agent_name,
                "item": item.item,
                "amount": item.amount,
                "position": {
                    "x": drop_position[0],
                    "y": drop_position[1],
                    "z": drop_position[2],
                },
                "response": str(response),
            }
        return True

    def _alive_agent_positions(
        self,
        server: Any,
    ) -> list[tuple[str, tuple[float, float, float]]]:
        candidates: list[tuple[str, tuple[float, float, float]]] = []
        names = self._runtime_agent_names or self._agent_names([])
        for name in names:
            try:
                health = _parse_health_value(
                    server.send_command(f"data get entity {name} Health")
                )
            except Exception:  # noqa: BLE001
                continue
            if health is None or health <= 0:
                continue
            try:
                position = _parse_position(server.send_command(f"data get entity {name} Pos"))
            except Exception:  # noqa: BLE001
                continue
            if position is not None:
                candidates.append((name, position))
        return candidates

    def _random_drop_position(
        self,
        origin: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        params = self.spec.params or {}
        min_distance = float(params.get("supply_drop_min_distance", 10.0))
        max_distance = float(params.get("supply_drop_max_distance", 20.0))
        distance = self._rng.uniform(min_distance, max_distance)
        angle = self._rng.uniform(0.0, math.tau)
        x, y, z = origin
        return (
            _round_coordinate(x + math.cos(angle) * distance),
            _round_coordinate(y + 1.0),
            _round_coordinate(z + math.sin(angle) * distance),
        )

    def _stop_supply_drop_thread(self) -> None:
        thread = self._supply_thread
        self._supply_stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=12.0)

    def _reset_runtime_state(self) -> None:
        self._last_system_message_at = None
        self._server = None
        self._agents = []
        self._runtime_agent_names = []
        self._supply_drop_items = ()
        self._supply_stop_event = threading.Event()
        self._supply_thread = None
        self._supply_error = None
        self._supply_drops_sent = 0
        self._last_supply_drop = None
        self._eliminated_agents = set()
        self._pending_eliminations = set()
        self._elimination_attempts = {}
        self._elimination_error = None
        self._last_query_note = None
        self._rng = random.Random()


_DIAMOND_GEAR = {
    "minecraft:diamond_sword",
    "minecraft:diamond_helmet",
    "minecraft:diamond_chestplate",
    "minecraft:diamond_leggings",
    "minecraft:diamond_boots",
}

_SCORE_RE = re.compile(r"\bhas(?: the following score)?[: ]+(-?\d+)")


def _parse_supply_drop_items(value: Any) -> tuple[SupplyDropItem, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskSpecError("params.supply_drop_items 必须是对象列表")
    items: list[SupplyDropItem] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise TaskSpecError(f"params.supply_drop_items[{index}] 必须是映射")
        item = _namespaced(str(raw.get("item") or ""))
        if not item:
            raise TaskSpecError(f"params.supply_drop_items[{index}].item 必填")
        if item in _DIAMOND_GEAR:
            raise TaskSpecError(
                f"params.supply_drop_items[{index}] 不允许投放钻石套装或钻石剑"
            )
        try:
            amount = int(raw.get("amount", 1))
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise TaskSpecError(
                f"params.supply_drop_items[{index}] amount/weight 非法"
            ) from exc
        if not 1 <= amount <= 127:
            raise TaskSpecError(
                f"params.supply_drop_items[{index}].amount 必须在 1..127"
            )
        if weight <= 0:
            raise TaskSpecError(
                f"params.supply_drop_items[{index}].weight 必须大于 0"
            )
        profile = str(raw.get("profile") or "default")
        tag = raw.get("tag")
        if tag is not None and not isinstance(tag, str):
            raise TaskSpecError(f"params.supply_drop_items[{index}].tag 必须是字符串")
        items.append(
            SupplyDropItem(
                item=item,
                amount=amount,
                weight=weight,
                profile=profile,
                tag=tag,
            )
        )
    if not items:
        raise TaskSpecError("params.supply_drop_items 不能为空")
    return tuple(items)


def _weighted_choice(
    rng: random.Random,
    items: Sequence[SupplyDropItem],
) -> SupplyDropItem:
    total = sum(item.weight for item in items)
    cursor = rng.uniform(0.0, total)
    upto = 0.0
    for item in items:
        upto += item.weight
        if cursor <= upto:
            return item
    return items[-1]


def _supply_summon_command(
    item: SupplyDropItem,
    position: tuple[float, float, float],
    *,
    supply_tag: str,
) -> str:
    x, y, z = position
    item_tag = _supply_item_tag(item)
    item_nbt = (
        f'Item:{{id:"{item.item}",Count:{item.amount}b'
        + (f",tag:{item_tag}" if item_tag else "")
        + "}"
    )
    nbt = (
        f'{{Tags:["{supply_tag}"],Age:-32768s,PickupDelay:0s,'
        f"Motion:[0.0d,0.0d,0.0d],{item_nbt}}}"
    )
    return f"summon minecraft:item {x:g} {y:g} {z:g} {nbt}"


def _supply_item_tag(item: SupplyDropItem) -> str | None:
    if item.tag:
        return item.tag
    if item.item == "minecraft:potion":
        potion = (
            "minecraft:strong_healing"
            if item.profile == "strong_healing"
            else "minecraft:healing"
        )
        return f'{{Potion:"{potion}"}}'
    return None


def _supply_entity_tag(spec: TaskSpec) -> str:
    params = spec.params or {}
    return str(params.get("supply_entity_tag") or "ac_boss_supply")


def _namespaced(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return text if ":" in text else f"minecraft:{text}"


def _round_coordinate(value: float) -> float:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return float(int(round(rounded)))
    return rounded


def _parse_position(response: Any) -> tuple[float, float, float] | None:
    if response is None:
        return None
    text = str(response)
    if "No entity was found" in text:
        return None
    match = re.search(r"entity data:\s*\[([^\]]+)\]", text)
    if not match:
        return None
    parts = [item.strip() for item in match.group(1).split(",")]
    if len(parts) != 3:
        return None
    values: list[float] = []
    for item in parts:
        item = re.sub(r"[dDfF]\s*$", "", item)
        try:
            values.append(float(item))
        except ValueError:
            return None
    return values[0], values[1], values[2]


def _parse_health_value(response: Any) -> float | None:
    if response is None:
        return None
    text = str(response)
    if "No entity was found" in text:
        return 0.0
    match = re.search(r"entity data:\s*([-+]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def _format_position(position: tuple[float, float, float]) -> str:
    x, y, z = position
    return f"({_format_number(x)}, {_format_number(y)}, {_format_number(z)})"


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


_SAFE_ITEM_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_/.-]+$")


def _context_snapshot(context: EvaluatorContext, name: str) -> dict[str, Any] | None:
    snapshot_method = getattr(context, "_snapshot", None)
    if callable(snapshot_method):
        try:
            snapshot = snapshot_method(name)
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    perception = getattr(context, "perception", None)
    if callable(perception):
        try:
            snapshot = perception(name)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(snapshot, dict):
            return snapshot
    return None


def _snapshot_game_mode(snapshot: dict[str, Any] | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("gameMode")
        if value is None:
            value = candidate.get("gamemode")
        if isinstance(value, str):
            text = value.strip().lower()
            if text:
                return text
    return None


def _collect_death_loot(
    context: EvaluatorContext,
    name: str,
) -> list[dict[str, Any]]:
    snapshot = _context_snapshot(context, name)
    raw_self = snapshot.get("self") if isinstance(snapshot, dict) else {}
    if not isinstance(raw_self, dict):
        raw_self = {}

    items: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    used_raw_inventory = False
    inventory = raw_self.get("inventory")
    if isinstance(inventory, list):
        used_raw_inventory = True
        for item in inventory:
            _append_loot_item(items, item, seen_slots=seen_slots)
    elif isinstance(inventory, dict):
        used_raw_inventory = True
        for item, count in inventory.items():
            _append_loot_item(
                items,
                {"name": item, "count": count},
                seen_slots=seen_slots,
            )

    equipment_slots = raw_self.get("equipmentSlots")
    if isinstance(equipment_slots, dict):
        for slot_name in ("mainHand", "offHand", "feet", "legs", "chest", "head"):
            _append_loot_item(
                items,
                equipment_slots.get(slot_name),
                seen_slots=seen_slots,
            )
    elif isinstance(raw_self.get("equipment"), list):
        for item in raw_self["equipment"][1:]:
            _append_loot_item(items, item, seen_slots=seen_slots)

    if not used_raw_inventory:
        inventory_method = getattr(context, "inventory", None)
        if callable(inventory_method):
            try:
                fallback_inventory = inventory_method(name)
            except Exception:  # noqa: BLE001
                fallback_inventory = None
            if isinstance(fallback_inventory, dict):
                for item, count in fallback_inventory.items():
                    _append_loot_item(
                        items,
                        {"name": item, "count": count},
                        seen_slots=seen_slots,
                    )
    return _aggregate_loot_items(items)


def _append_loot_item(
    items: list[dict[str, Any]],
    item: Any,
    *,
    seen_slots: set[int],
) -> None:
    if not isinstance(item, dict):
        return
    slot = _int_or_none(item.get("slot"))
    if slot is not None:
        if slot in seen_slots:
            return
        seen_slots.add(slot)
    raw_id = item.get("id") or item.get("name") or item.get("type")
    item_id = _minecraft_item_id(raw_id)
    count = _int_or_none(item.get("count"))
    if item_id is None or count is None or count <= 0:
        return
    nbt = item.get("nbt") if item.get("nbt") is not None else item.get("tag")
    items.append({"id": item_id, "count": count, "nbt": nbt})


def _aggregate_loot_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
    for item in items:
        fingerprint = (
            json.dumps(item.get("nbt"), ensure_ascii=True, sort_keys=True, default=str)
            if item.get("nbt") is not None
            else None
        )
        key = (item["id"], fingerprint)
        if key not in grouped:
            grouped[key] = {"id": item["id"], "count": 0, "nbt": item.get("nbt")}
        grouped[key]["count"] += int(item["count"])
    return list(grouped.values())


def _minecraft_item_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        text = f"minecraft:{text}"
    if not _SAFE_ITEM_ID_RE.fullmatch(text):
        return None
    return text


def _position_tuple(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return (
            float(value["x"]),
            float(value["y"]),
            float(value["z"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _split_stack_count(count: int) -> list[int]:
    remaining = max(0, int(count))
    result: list[int] = []
    while remaining > 0:
        current = min(64, remaining)
        result.append(current)
        remaining -= current
    return result


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _snbt(value: Any) -> str:
    if isinstance(value, Mapping):
        entries = [
            f"{_snbt_key(str(key))}:{_snbt(item)}"
            for key, item in value.items()
            if item is not None
        ]
        return "{" + ",".join(entries) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_snbt(item) for item in value) + "]"
    if isinstance(value, bool):
        return "1b" if value else "0b"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return _format_number(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _snbt_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
