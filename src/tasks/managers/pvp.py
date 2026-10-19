"""Two-team PVP task manager."""

from __future__ import annotations

from dataclasses import dataclass
import json
import random
import re
import threading
import time
from typing import Any, Callable, Mapping

from src.runtime.liveness import set_agent_live_by_name
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
from src.tasks.pvp.schedule import (
    PVPDropConfig,
    PVPDropRecord,
    PVPSupplyError,
    class_probabilities,
    gear_probability,
    generate_drop_batch,
    generate_drop_record,
    summon_drop_command,
)
from src.tasks.spec import TaskSpec, TaskSpecError
from src.tasks.system_messages import publish_system_message


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SAFE_ITEM_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_SCORE_RE = re.compile(r"\bhas(?: the following score)?[: ]+(-?\d+)")
_PVP_INITIAL_INVENTORY = {
    "minecraft:wooden_sword": 1,
    "minecraft:bow": 1,
}
_HOSTILE_TYPES = (
    "minecraft:blaze",
    "minecraft:cave_spider",
    "minecraft:creeper",
    "minecraft:drowned",
    "minecraft:enderman",
    "minecraft:endermite",
    "minecraft:evoker",
    "minecraft:ghast",
    "minecraft:guardian",
    "minecraft:hoglin",
    "minecraft:husk",
    "minecraft:magma_cube",
    "minecraft:phantom",
    "minecraft:piglin_brute",
    "minecraft:pillager",
    "minecraft:ravager",
    "minecraft:shulker",
    "minecraft:silverfish",
    "minecraft:skeleton",
    "minecraft:slime",
    "minecraft:spider",
    "minecraft:stray",
    "minecraft:vex",
    "minecraft:vindicator",
    "minecraft:warden",
    "minecraft:witch",
    "minecraft:wither_skeleton",
    "minecraft:zoglin",
    "minecraft:zombie",
    "minecraft:zombie_villager",
)
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class PVPTeam:
    team_id: str
    agents: tuple[str, ...]


def pvp_team_rosters(spec: TaskSpec | Mapping[str, Any]) -> tuple[PVPTeam, PVPTeam]:
    data = spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)
    agents = data.get("agents") or {}
    teams = agents.get("teams") or []
    result: list[PVPTeam] = []
    for entry in teams:
        if not isinstance(entry, Mapping):
            continue
        team_id = str(entry.get("id") or entry.get("name") or "").strip()
        members = entry.get("agents") or entry.get("members") or []
        result.append(
            PVPTeam(
                team_id=team_id,
                agents=tuple(str(name) for name in members),
            )
        )
    if len(result) != 2:
        raise TaskSpecError("pvp 要求 agents.teams 恰好配置两支队伍")
    return result[0], result[1]


class PVPManager(BaseTaskManager):
    """Manage a symmetric two-team PVP battle and its live supply drops."""

    def __init__(
        self,
        *,
        supply_thread_enabled: bool = True,
        death_monitor_enabled: bool | None = None,
        monotonic: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        super().__init__()
        self._supply_thread_enabled = supply_thread_enabled
        self._death_monitor_enabled = (
            supply_thread_enabled
            if death_monitor_enabled is None
            else death_monitor_enabled
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._server: Any = None
        self._memory: Any = None
        self._agents: list[Any] = []
        self._teams: tuple[PVPTeam, PVPTeam] = ()
        self._team_by_agent: dict[str, str] = {}
        self._drop_config: PVPDropConfig | None = None
        self._rng = random.Random()
        self._supply_thread: threading.Thread | None = None
        self._supply_stop = threading.Event()
        self._death_monitor_thread: threading.Thread | None = None
        self._death_monitor_stop = threading.Event()
        self._state_lock = threading.RLock()
        self._terminal_outcome: TaskOutcome | None = None
        self._supply_error: Exception | None = None
        self._spawned_supplies = 0
        self._spawned_supply_waves = 0
        self._last_drop: PVPDropRecord | None = None
        self._last_drop_batch: list[PVPDropRecord] = []
        self._eliminated_agents: set[str] = set()
        self._pending_eliminations: set[str] = set()
        self._elimination_attempts: dict[str, int] = {}
        self._elimination_error: dict[str, str] | None = None
        self._death_positions: dict[str, tuple[float, float, float]] = {}
        self._death_loot_dropped: set[str] = set()
        self._death_announced_agents: set[str] = set()
        self._last_query_note: str | None = None
        self._last_hunger_elapsed = 300.0
        self._last_oob_elapsed = 0.0
        self._last_monster_cleanup_elapsed = 0.0
        self._death_objective = "acPvpDeath"
        self._alive_objective = "acPvpAlive"
        self._supply_interval_seconds = 30.0
        self._hunger_after_seconds = 300.0
        self._hunger_interval_seconds = 10.0
        self._hunger_effect_seconds = 10
        self._hunger_effect_amplifier = 3
        self._out_of_bounds_interval_seconds = 10.0
        self._out_of_bounds_damage = 1.0
        self._out_of_bounds_y_padding = 6
        self._monster_cleanup_interval_seconds = 5.0
        self._death_monitor_interval_seconds = 0.2
        self._death_drops_enabled = True
        self._death_drop_tag = "ac_pvp_death"
        self._started_monotonic: float | None = None

    def configure(self, spec: TaskSpec) -> None:
        if self._supply_thread is not None and self._supply_thread.is_alive():
            raise TaskSpecError("PVP 掉落线程仍在运行，请先 teardown")
        if (
            self._death_monitor_thread is not None
            and self._death_monitor_thread.is_alive()
        ):
            raise TaskSpecError("PVP death monitor is still running; call teardown first")
        self._reset_runtime_state()
        super().configure(spec)

    def _validate_spec(self, spec: TaskSpec) -> None:
        if spec.category != "pvp":
            raise TaskSpecError("PVPManager 仅支持 pvp")
        names = list(spec.agents.get("names") or [])
        count = int(spec.agents.get("count", 0))
        if (
            count < 2
            or len(names) != count
            or len(set(names)) != len(names)
        ):
            raise TaskSpecError("pvp 要求 agents.count >= 2 且 names 唯一并与 count 一致")
        teams = pvp_team_rosters(spec)
        assigned = [name for team in teams for name in team.agents]
        if sorted(assigned) != sorted(names) or len(set(assigned)) != len(assigned):
            raise TaskSpecError("pvp 要求两队成员完整且不重复覆盖 agents.names")
        for name in assigned:
            _validate_command_name(name, f"agent {name}")
        if any(not team.agents for team in teams):
            raise TaskSpecError("pvp 每支队伍至少需要 1 个 Agent")
        for team in teams:
            _validate_command_name(team.team_id, f"team {team.team_id}")
        success = spec.completion.get("success") or {}
        if success.get("type") != "team_eliminated":
            raise TaskSpecError("pvp 要求 success.type=team_eliminated")
        failure = spec.completion.get("failure") or {}
        if failure and failure.get("type") != "all_agents_dead":
            raise TaskSpecError("pvp failure.type 仅支持 all_agents_dead")
        if spec.spawns:
            raise TaskSpecError("pvp 不允许配置怪物或其他初始 spawns")
        self._validate_initial_loadout(spec)
        self._load_params(spec)
        self._teams = teams
        self._team_by_agent = {
            agent: team.team_id for team in teams for agent in team.agents
        }

    def _validate_initial_loadout(self, spec: TaskSpec) -> None:
        state = spec.initial_state or {}
        shared = state.get("per_agent") or {}
        inventory = shared.get("inventory") or {}
        if not isinstance(inventory, Mapping) or dict(inventory) != _PVP_INITIAL_INVENTORY:
            raise TaskSpecError("pvp 初始背包必须且只能包含木剑与弓各 1 个")
        if shared.get("equipment"):
            raise TaskSpecError("pvp 初始不得配置已装备物品")
        positions = state.get("by_agent") or []
        positioned = []
        for block in positions:
            if block.get("inventory") or block.get("equipment"):
                raise TaskSpecError("pvp 初始不得为单个 Agent 分配物品或装备")
            if "position" not in block:
                raise TaskSpecError("pvp 必须为每个 Agent 配置出生点")
            positioned.append(str(block.get("name")))
        if sorted(positioned) != sorted(spec.agents.get("names") or []):
            raise TaskSpecError("pvp 必须为每个 Agent 配置且仅配置一个出生点")

    def _load_params(self, spec: TaskSpec) -> None:
        params = spec.params or {}
        try:
            drop_config = PVPDropConfig.from_params(params)
            supply_interval = float(params.get("supply_interval_seconds", 30.0))
            hunger_after = float(params.get("hunger_pressure_after_seconds", 300.0))
            hunger_interval = float(params.get("hunger_interval_seconds", 10.0))
            hunger_duration = int(params.get("hunger_effect_seconds", 10))
            hunger_amplifier = int(params.get("hunger_effect_amplifier", 3))
            oob_interval = float(params.get("out_of_bounds_interval_seconds", 10.0))
            oob_damage = float(params.get("out_of_bounds_damage", 1.0))
            y_padding = int(params.get("out_of_bounds_y_padding", 6))
            cleanup_interval = float(
                params.get("monster_cleanup_interval_seconds", 5.0)
            )
            death_monitor_interval = float(
                params.get("death_monitor_interval_seconds", 0.2)
            )
            death_drops_enabled = _bool_param(params, "death_drops_enabled", True)
        except (PVPSupplyError, TypeError, ValueError) as exc:
            raise TaskSpecError(f"pvp 参数非法: {exc}") from exc
        if supply_interval <= 0:
            raise TaskSpecError("pvp 掉落间隔必须大于 0")
        if hunger_after < 0 or hunger_interval <= 0:
            raise TaskSpecError("pvp 饥饿压力时间参数非法")
        if hunger_duration < 1 or hunger_amplifier < 0:
            raise TaskSpecError("pvp 饥饿效果参数非法")
        if oob_interval <= 0 or oob_damage <= 0 or y_padding < 0:
            raise TaskSpecError("pvp 出界惩罚参数非法")
        if cleanup_interval <= 0:
            raise TaskSpecError("pvp 怪物清理间隔必须大于 0")
        if death_monitor_interval <= 0:
            raise TaskSpecError("pvp death monitor interval must be positive")
        death_objective = str(params.get("death_objective") or "acPvpDeath")
        alive_objective = str(params.get("alive_objective") or "acPvpAlive")
        death_drop_tag = str(params.get("death_drop_tag") or "ac_pvp_death")
        for label, value in (
            ("supply_tag", drop_config.supply_tag),
            ("death_objective", death_objective),
            ("alive_objective", alive_objective),
            ("death_drop_tag", death_drop_tag),
        ):
            _validate_command_name(value, f"params.{label}")
            if label.endswith("objective") and len(value) > 16:
                raise TaskSpecError("scoreboard objective 名称不能超过 16 个字符")
        self._drop_config = drop_config
        self._supply_interval_seconds = supply_interval
        self._hunger_after_seconds = hunger_after
        self._hunger_interval_seconds = hunger_interval
        self._hunger_effect_seconds = hunger_duration
        self._hunger_effect_amplifier = hunger_amplifier
        self._out_of_bounds_interval_seconds = oob_interval
        self._out_of_bounds_damage = oob_damage
        self._out_of_bounds_y_padding = y_padding
        self._monster_cleanup_interval_seconds = cleanup_interval
        self._death_monitor_interval_seconds = death_monitor_interval
        self._death_objective = death_objective
        self._alive_objective = alive_objective
        self._death_drops_enabled = death_drops_enabled
        self._death_drop_tag = death_drop_tag
        self._last_hunger_elapsed = hunger_after
        seed = int(params.get("generation_seed", 0))
        self._rng = random.Random(seed)

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        actual_names = self._agent_names(agents)
        expected_names = self._roster_agent_names()
        duplicates = sorted(
            name for name in set(actual_names) if actual_names.count(name) > 1
        )
        if duplicates:
            raise TaskSpecError(f"PVP Agent 名称重复: {duplicates}")
        agent_by_name = {
            str(getattr(agent, "username", agent)): agent for agent in agents
        }
        missing = [name for name in expected_names if name not in agent_by_name]
        if missing:
            raise TaskSpecError(
                f"缺少 PVP 队伍 Agent: expected={expected_names}, "
                f"actual={actual_names}, missing={missing}"
            )
        team_agents = [agent_by_name[name] for name in expected_names]
        self._server = server
        self._memory = memory
        self._agents = list(team_agents)
        self._prepare_scoreboards_teams_and_field(
            server,
            expected_names,
        )
        super().prepare(server, team_agents, memory)

    def _prepare_scoreboards_teams_and_field(
        self,
        server: Any,
        names: list[str],
    ) -> None:
        commands = [
            *self._clear_field_commands(),
            f"kill @e[tag={self._drop_config.supply_tag if self._drop_config else 'ac_pvp_sup'}]",
            f"kill @e[tag={self._death_drop_tag}]",
            f"scoreboard objectives remove {self._death_objective}",
            f"scoreboard objectives add {self._death_objective} deathCount",
            f"scoreboard objectives remove {self._alive_objective}",
            f"scoreboard objectives add {self._alive_objective} dummy",
            "gamerule doMobSpawning false",
            "gamerule mobGriefing false",
            "gamerule naturalRegeneration false",
        ]
        for team in self._teams:
            commands.append(f"team remove {team.team_id}")
            commands.append(f"team add {team.team_id}")
            commands.append(f"team modify {team.team_id} friendlyFire false")
            commands.append(f"team modify {team.team_id} seeFriendlyInvisibles true")
            for name in team.agents:
                commands.append(f"team join {team.team_id} {name}")
        non_roster_leave = self._non_roster_team_leave_command(names)
        if non_roster_leave:
            commands.append(non_roster_leave)
        for name in names:
            commands.append(f"scoreboard players set {name} {self._death_objective} 0")
            commands.append(f"scoreboard players set {name} {self._alive_objective} 1")
            commands.append(f"clear {name}")
            commands.append(f"effect clear {name}")
            commands.append(f"gamemode survival {name}")
        self._apply_commands(server, commands)

    def _clear_field_commands(self) -> list[str]:
        if self._drop_config is None:
            return []
        bounds = self._drop_config.bounds
        return [
            f"forceload add "
            f"{bounds.min_x // 16} {bounds.min_z // 16} "
            f"{bounds.max_x // 16} {bounds.max_z // 16}",
            f"kill @e[type=!player,"
            f"x={bounds.min_x},y={bounds.min_y},z={bounds.min_z},"
            f"dx={bounds.max_x - bounds.min_x},"
            f"dy={bounds.max_y - bounds.min_y + self._out_of_bounds_y_padding},"
            f"dz={bounds.max_z - bounds.min_z}]",
            "forceload remove all",
        ]

    def start(self) -> None:
        super().start()
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        self._started_monotonic = self._monotonic()
        if self._death_monitor_enabled:
            self._death_monitor_stop = threading.Event()
            self._death_monitor_thread = threading.Thread(
                target=self._run_death_monitor_loop,
                name=f"pvp-deaths-{self.spec.task_id if self.spec else 'task'}",
                daemon=True,
            )
            self._death_monitor_thread.start()
        if self._supply_thread_enabled:
            self._supply_thread = threading.Thread(
                target=self._run_supply_loop,
                name=f"pvp-supplies-{self.spec.task_id if self.spec else 'task'}",
                daemon=True,
            )
            self._supply_thread.start()

    def _run_supply_loop(self) -> None:
        next_spawn_elapsed = 0.0
        while not self._supply_stop.is_set():
            start = self._started_monotonic
            if start is None:
                return
            elapsed = max(0.0, self._monotonic() - start)
            if elapsed + 1e-9 >= next_spawn_elapsed:
                try:
                    self.spawn_supply_once(elapsed_seconds=elapsed)
                except Exception as exc:  # noqa: BLE001
                    with self._state_lock:
                        self._supply_error = exc
                    return
                next_spawn_elapsed += self._supply_interval_seconds
            wait_for = max(0.05, min(0.25, next_spawn_elapsed - elapsed))
            self._supply_stop.wait(wait_for)

    def _run_death_monitor_loop(self) -> None:
        while not self._death_monitor_stop.is_set():
            server = self._server
            if server is None:
                return
            for name in self._uneliminated_agent_names():
                score = self._query_score(
                    server,
                    name,
                    self._death_objective,
                    priority=True,
                )
                if death_score_indicates_elimination(score):
                    self._handle_agent_eliminated(
                        name,
                        source="death_monitor",
                    )
            self._death_monitor_stop.wait(self._death_monitor_interval_seconds)

    def _uneliminated_agent_names(self) -> list[str]:
        with self._state_lock:
            eliminated = set(self._eliminated_agents)
        return [
            name for name in self._roster_agent_names() if name not in eliminated
        ]

    def spawn_supply_once(
        self,
        *,
        elapsed_seconds: float | None = None,
    ) -> tuple[list[PVPDropRecord], list[str], list[str]]:
        if self._server is None or self._drop_config is None:
            raise TaskSpecError("PVP manager 尚未 prepare")
        elapsed = (
            max(0.0, float(elapsed_seconds))
            if elapsed_seconds is not None
            else self._current_elapsed_seconds()
        )
        with self._state_lock:
            index = self._spawned_supplies + 1
            records = generate_drop_batch(
                config=self._drop_config,
                rng=self._rng,
                spawn_index_start=index,
                elapsed_seconds=elapsed,
            )
            commands = [
                summon_drop_command(
                    record,
                    supply_tag=self._drop_config.supply_tag,
                )
                for record in records
            ]
        responses = [self._rcon(self._server, command) for command in commands]
        with self._state_lock:
            self._spawned_supplies += len(records)
            self._spawned_supply_waves += 1
            self._last_drop = records[-1] if records else None
            self._last_drop_batch = list(records)
        return records, commands, responses

    def spawn_supply_item_once(
        self,
        *,
        elapsed_seconds: float | None = None,
    ) -> tuple[PVPDropRecord, str, str]:
        if self._server is None or self._drop_config is None:
            raise TaskSpecError("PVP manager 尚未 prepare")
        elapsed = (
            max(0.0, float(elapsed_seconds))
            if elapsed_seconds is not None
            else self._current_elapsed_seconds()
        )
        with self._state_lock:
            index = self._spawned_supplies + 1
            record = generate_drop_record(
                config=self._drop_config,
                rng=self._rng,
                spawn_index=index,
                elapsed_seconds=elapsed,
            )
            command = summon_drop_command(
                record,
                supply_tag=self._drop_config.supply_tag,
            )
        response = self._rcon(self._server, command)
        with self._state_lock:
            self._spawned_supplies += 1
            self._spawned_supply_waves += 1
            self._last_drop = record
            self._last_drop_batch = [record]
        return record, command, response

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if not self._started:
            raise TaskSpecError("任务管理器尚未 start")
        self._last_query_note = None
        with self._state_lock:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
            supply_error = self._supply_error
        if supply_error is not None:
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="supply_failed",
                    detail={"error": str(supply_error)},
                )
            )

        self._update_agent_eliminations(context)
        if self._elimination_error is not None:
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="agent_elimination_failed",
                    detail=dict(self._elimination_error),
                )
            )
        self._apply_periodic_rules(context)
        status = self._team_status()
        alive_teams = [
            team_id
            for team_id, data in status.items()
            if data["alive_count"] > 0
        ]
        eliminated_teams = [
            team_id
            for team_id, data in status.items()
            if data["alive_count"] == 0
        ]
        if not alive_teams:
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="all_agents_dead",
                    detail=self._status_metrics(context, status),
                )
            )
        if eliminated_teams:
            winner = alive_teams[0] if len(alive_teams) == 1 else None
            return self._latch(
                TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="team_eliminated",
                    detail={
                        **self._status_metrics(context, status),
                        "winner": winner,
                        "eliminated_teams": eliminated_teams,
                    },
                )
            )
        return TaskOutcome(
            TaskStatus.RUNNING,
            detail=self._status_metrics(context, status),
        )

    def _update_agent_eliminations(self, context: EvaluatorContext) -> None:
        if self._server is None:
            return
        for name in self._roster_agent_names():
            if name in self._eliminated_agents:
                self._drop_death_loot_once(
                    context,
                    name,
                    self._stored_death_position(name),
                )
                continue
            score = self._query_score(
                self._server,
                name,
                self._death_objective,
                priority=True,
            )
            snapshot = _context_snapshot(context, name)
            game_mode = snapshot_game_mode(snapshot)
            dead = game_mode == "spectator" if game_mode is not None else None
            if score is None:
                self._last_query_note = f"failed to read death score for {name}"
            if score is None and dead is None:
                self._last_query_note = (
                    f"failed to read death score or health for {name}"
                )
                continue
            if death_score_indicates_elimination(score) or dead is True:
                self._handle_agent_eliminated(
                    name,
                    context=context,
                    snapshot=snapshot,
                    source="completion_check",
                )

    def _handle_agent_eliminated(
        self,
        name: str,
        *,
        context: EvaluatorContext | None = None,
        snapshot: Mapping[str, Any] | None = None,
        position: tuple[float, float, float] | None = None,
        source: str = "unknown",
    ) -> None:
        if position is None:
            position = self._agent_position_from_context(context, name) if context is not None else None
        with self._state_lock:
            if name in self._eliminated_agents:
                first_seen = False
            elif name in self._pending_eliminations:
                return
            else:
                self._pending_eliminations.add(name)
                first_seen = True
        set_agent_live_by_name(self._agents, name, False)
        spectator_ok, spectator_error = self._set_agent_spectator(
            name,
            context=context,
            snapshot=snapshot,
        )
        if not spectator_ok:
            with self._state_lock:
                self._pending_eliminations.discard(name)
                attempts = self._elimination_attempts.get(name, 0) + 1
                self._elimination_attempts[name] = attempts
                self._last_query_note = (
                    f"failed to apply PVP elimination command for {name}: "
                    f"{spectator_error}"
                )
                if attempts >= self.max_elimination_attempts:
                    self._elimination_error = {
                        "agent": name,
                        "error": spectator_error or "无法确认旁观者模式",
                        "attempts": str(attempts),
                    }
            return
        if position is None:
            position = self._agent_position_from_server(name)
        with self._state_lock:
            self._pending_eliminations.discard(name)
            self._elimination_attempts.pop(name, None)
            if position is not None and name not in self._death_positions:
                self._death_positions[name] = position
            self._eliminated_agents.add(name)
        if first_seen:
            self._publish_death_announcement_once(name, position, source=source)
        if context is not None:
            self._drop_death_loot_once(context, name, position)

    def _set_agent_spectator(
        self,
        name: str,
        *,
        context: EvaluatorContext | None = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        if self._server is None:
            return False, "任务服务器不可用"
        try:
            response = send_priority_command(
                self._server,
                spectator_score_selector(name, self._death_objective)
            )
            self._server.send_command(
                f"scoreboard players set {name} {self._alive_objective} 0"
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        verified_snapshot = snapshot
        if context is not None:
            verified_snapshot = _context_snapshot(context, name) or snapshot
        return spectator_command_verified(response, verified_snapshot)

    def _publish_death_announcement_once(
        self,
        name: str,
        position: tuple[float, float, float] | None,
        *,
        source: str,
    ) -> None:
        with self._state_lock:
            if name in self._death_announced_agents:
                return
            self._death_announced_agents.add(name)
        self._publish_death_announcement(name, position, source=source)

    def _drop_death_loot_once(
        self,
        context: EvaluatorContext,
        name: str,
        position: tuple[float, float, float] | None,
    ) -> None:
        with self._state_lock:
            if name in self._death_loot_dropped:
                return
            position = position or self._death_positions.get(name)
        if position is None:
            position = self._agent_position(context, name)
        if position is not None:
            with self._state_lock:
                self._death_positions.setdefault(name, position)
        if self._server is None:
            return
        if self._death_drops_enabled and position is not None:
            for command in self._death_loot_commands(context, name, position):
                try:
                    self._server.send_command(command)
                except Exception as exc:  # noqa: BLE001
                    self._last_query_note = (
                        f"failed to summon PVP death loot for {name}: {exc}"
                    )
        elif self._death_drops_enabled:
            self._last_query_note = (
                f"failed to read death position for {name}; skipped PVP death loot"
            )
        try:
            self._server.send_command(f"clear {name}")
        except Exception as exc:  # noqa: BLE001
            self._last_query_note = f"failed to clear PVP death inventory for {name}: {exc}"
        with self._state_lock:
            self._death_loot_dropped.add(name)

    def _stored_death_position(
        self,
        name: str,
    ) -> tuple[float, float, float] | None:
        with self._state_lock:
            return self._death_positions.get(name)

    def _agent_position_from_server(
        self,
        name: str,
    ) -> tuple[float, float, float] | None:
        if self._server is None:
            return None
        try:
            return _parse_position(
                self._rcon(self._server, f"data get entity {name} Pos")
            )
        except Exception:  # noqa: BLE001
            return None

    def _agent_position_from_context(
        self,
        context: EvaluatorContext,
        name: str,
    ) -> tuple[float, float, float] | None:
        snapshot = _context_snapshot(context, name)
        if isinstance(snapshot, dict):
            self_data = snapshot.get("self")
            if isinstance(self_data, dict):
                position = _position_tuple(self_data.get("position"))
                if position is not None:
                    return position
            position = _position_tuple(snapshot.get("position"))
            if position is not None:
                return position
        return None

    def _agent_position(
        self,
        context: EvaluatorContext,
        name: str,
    ) -> tuple[float, float, float] | None:
        if self._server is not None:
            try:
                position = _parse_position(
                    self._rcon(self._server, f"data get entity {name} Pos")
                )
            except Exception:  # noqa: BLE001
                position = None
            if position is not None:
                return position
        return self._agent_position_from_context(context, name)

    def _drop_death_loot(
        self,
        context: EvaluatorContext,
        name: str,
        position: tuple[float, float, float] | None,
    ) -> None:
        if self._server is None:
            return
        try:
            if self._death_drops_enabled and position is not None:
                for command in self._death_loot_commands(context, name, position):
                    self._server.send_command(command)
            elif self._death_drops_enabled:
                self._last_query_note = f"无法读取 {name} 死亡位置，跳过死亡掉落"
            self._server.send_command(f"clear {name}")
        except Exception as exc:  # noqa: BLE001
            self._last_query_note = f"处理 {name} 死亡掉落失败: {exc}"

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
                    f'{{Tags:["{self._death_drop_tag}","ac_pvp_death_{name}_{index}"],'
                    f"Age:-32768s,PickupDelay:0s,Motion:[0.0d,0.0d,0.0d],{item_nbt}}}"
                )
                commands.append(f"summon minecraft:item {x} {y} {z} {nbt}")
        return commands

    def _publish_death_announcement(
        self,
        name: str,
        position: tuple[float, float, float] | None,
        *,
        source: str = "completion_check",
    ) -> None:
        if self._server is None:
            return
        message = f"{name}（{_format_position_for_message(position)}）死亡。"
        result = publish_system_message(
            message,
            memory=self._memory,
            broadcast=lambda text: self._server.send_command(f"say {text}"),
            sender="system",
            metadata={
                "source": "task_manager.pvp_death",
                "agent": name,
                "detected_by": source,
            },
        )
        if not result.wrote_memory and not result.broadcasted:
            self._last_query_note = f"无法发布 {name} 死亡公告"

    def _apply_periodic_rules(self, context: EvaluatorContext) -> None:
        if self._server is None or self._drop_config is None:
            return
        elapsed = max(0.0, float(context.elapsed_seconds()))
        if elapsed - self._last_monster_cleanup_elapsed >= self._monster_cleanup_interval_seconds:
            self._run_best_effort_commands(
                self._hostile_cleanup_commands()
            )
            self._last_monster_cleanup_elapsed = elapsed
        if elapsed - self._last_oob_elapsed >= self._out_of_bounds_interval_seconds:
            self._run_best_effort_commands(self._out_of_bounds_penalty_commands())
            self._last_oob_elapsed = elapsed
        if (
            elapsed > self._hunger_after_seconds
            and elapsed - self._last_hunger_elapsed >= self._hunger_interval_seconds
        ):
            self._run_best_effort_commands(self._sudden_death_pressure_commands())
            self._last_hunger_elapsed = elapsed

    def _hostile_cleanup_commands(self) -> list[str]:
        if self._drop_config is None:
            return []
        bounds = self._drop_config.bounds
        return [
            f"kill @e[type={entity_type},"
            f"x={bounds.min_x},y={bounds.min_y},z={bounds.min_z},"
            f"dx={bounds.max_x - bounds.min_x},"
            f"dy={bounds.max_y - bounds.min_y + self._out_of_bounds_y_padding},"
            f"dz={bounds.max_z - bounds.min_z}]"
            for entity_type in _HOSTILE_TYPES
        ]

    def _out_of_bounds_penalty_commands(self) -> list[str]:
        effect_seconds = self._wither_effect_seconds(
            self._out_of_bounds_interval_seconds
        )
        return [
            (
                f"execute unless entity {self._in_bounds_selector(name)} run "
                f"effect give {name} minecraft:wither {effect_seconds} 0 true"
            )
            for name in self._alive_agents()
        ]

    def _wither_effect_seconds(self, interval_seconds: float) -> int:
        return max(
            2,
            int(round(self._out_of_bounds_damage * 2)),
            int(float(interval_seconds)) + 1,
        )

    def _in_bounds_selector(self, name: str) -> str:
        if self._drop_config is None:
            return f"@a[name={name}]"
        bounds = self._drop_config.bounds
        y = bounds.min_y - self._out_of_bounds_y_padding
        dy = bounds.max_y - bounds.min_y + 2 * self._out_of_bounds_y_padding
        return (
            f"@a[name={name},x={bounds.min_x},y={y},z={bounds.min_z},"
            f"dx={bounds.max_x - bounds.min_x},dy={dy},"
            f"dz={bounds.max_z - bounds.min_z}]"
        )

    def _hunger_pressure_commands(self) -> list[str]:
        return [
            (
                f"effect give {name} minecraft:hunger "
                f"{self._hunger_effect_seconds} {self._hunger_effect_amplifier} true"
            )
            for name in self._alive_agents()
        ]

    def _sudden_death_wither_commands(self) -> list[str]:
        effect_seconds = self._wither_effect_seconds(self._hunger_interval_seconds)
        return [
            f"effect give {name} minecraft:wither {effect_seconds} 0 true"
            for name in self._alive_agents()
        ]

    def _sudden_death_pressure_commands(self) -> list[str]:
        return [
            *self._hunger_pressure_commands(),
            *self._sudden_death_wither_commands(),
        ]

    def _run_best_effort_commands(self, commands: list[str]) -> None:
        if self._server is None:
            return
        for command in commands:
            try:
                self._server.send_command(command)
            except Exception as exc:  # noqa: BLE001
                self._last_query_note = f"周期命令失败: {exc}"

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

    def _team_status(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for team in self._teams:
            eliminated = [
                name for name in team.agents if name in self._eliminated_agents
            ]
            alive = [
                name for name in team.agents if name not in self._eliminated_agents
            ]
            result[team.team_id] = {
                "agents": list(team.agents),
                "alive_agents": alive,
                "eliminated_agents": eliminated,
                "alive_count": len(alive),
                "eliminated_count": len(eliminated),
            }
        return result

    def _alive_agents(self) -> list[str]:
        return [
            name
            for name in self._roster_agent_names()
            if name not in self._eliminated_agents
        ]

    def _roster_agent_names(self) -> list[str]:
        return [name for team in self._teams for name in team.agents]

    def _non_roster_team_leave_command(self, roster_names: list[str]) -> str | None:
        if not roster_names:
            return None
        selector_args = ",".join(f"name=!{name}" for name in roster_names)
        return f"team leave @a[{selector_args}]"

    def _status_metrics(
        self,
        context: EvaluatorContext,
        status: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        elapsed = max(0.0, float(context.elapsed_seconds()))
        drop_config = self._drop_config
        metrics: dict[str, Any] = {
            "elapsed_seconds": elapsed,
            "teams": status or self._team_status(),
            "alive_agents": self._alive_agents(),
            "eliminated_agents": sorted(self._eliminated_agents),
            "pending_eliminations": sorted(self._pending_eliminations),
            "elimination_error": (
                dict(self._elimination_error) if self._elimination_error else None
            ),
            "spawned_supplies": self._spawned_supplies,
            "spawned_supply_waves": self._spawned_supply_waves,
            "last_drop": self._last_drop.to_dict() if self._last_drop else None,
            "last_drop_batch": [
                record.to_dict() for record in self._last_drop_batch
            ],
            "sudden_death_active": elapsed > self._hunger_after_seconds,
            "hunger_pressure_after_seconds": self._hunger_after_seconds,
            "hunger_interval_seconds": self._hunger_interval_seconds,
            "out_of_bounds_interval_seconds": self._out_of_bounds_interval_seconds,
            "out_of_bounds_damage": self._out_of_bounds_damage,
        }
        if drop_config is not None:
            metrics["bounds"] = drop_config.bounds.to_dict()
            metrics["gear_probability"] = gear_probability(elapsed, drop_config)
            metrics["drop_class_probabilities"] = class_probabilities(
                elapsed,
                drop_config,
            )
            metrics["food_healing_disabled"] = (
                elapsed >= drop_config.food_healing_disabled_after_seconds
            )
        if self._last_query_note:
            metrics["query_note"] = self._last_query_note
        return metrics

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        return self._status_metrics(context)

    def system_message(self, context: EvaluatorContext) -> str | None:
        elapsed = max(0.0, float(context.elapsed_seconds()))
        if elapsed < self._hunger_after_seconds:
            return None
        return (
            "PVP sudden death is active: every 10 seconds agents receive hunger "
            "and wither pressure. Leaving the arena also applies sustained wither pressure."
        )

    def teardown(self) -> None:
        self._death_monitor_stop.set()
        death_thread = self._death_monitor_thread
        if (
            death_thread is not None
            and death_thread.is_alive()
            and death_thread is not threading.current_thread()
        ):
            death_thread.join(timeout=12.0)
        self._supply_stop.set()
        thread = self._supply_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=12.0)
        server = self._server
        names = self._roster_agent_names()
        if server is not None:
            commands = []
            if self._drop_config is not None:
                commands.append(f"kill @e[tag={self._drop_config.supply_tag}]")
            commands.append(f"kill @e[tag={self._death_drop_tag}]")
            commands.extend(f"gamemode survival {name}" for name in names)
            commands.extend(
                (
                    f"scoreboard objectives remove {self._death_objective}",
                    f"scoreboard objectives remove {self._alive_objective}",
                )
            )
            commands.extend(f"team remove {team.team_id}" for team in self._teams)
            for command in commands:
                try:
                    server.send_command(command)
                except Exception:  # noqa: BLE001
                    pass
        self._reset_runtime_state()
        super().teardown()

    def _current_elapsed_seconds(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return max(0.0, self._monotonic() - self._started_monotonic)

    def _latch(self, outcome: TaskOutcome) -> TaskOutcome:
        with self._state_lock:
            if self._terminal_outcome is None:
                self._terminal_outcome = outcome
            return self._terminal_outcome

    def _reset_runtime_state(self) -> None:
        self._server = None
        self._memory = None
        self._agents = []
        self._teams = ()
        self._team_by_agent = {}
        self._drop_config = None
        self._rng = random.Random()
        self._supply_thread = None
        self._supply_stop = threading.Event()
        self._death_monitor_thread = None
        self._death_monitor_stop = threading.Event()
        self._terminal_outcome = None
        self._supply_error = None
        self._spawned_supplies = 0
        self._spawned_supply_waves = 0
        self._last_drop = None
        self._last_drop_batch = []
        self._eliminated_agents = set()
        self._pending_eliminations = set()
        self._elimination_attempts = {}
        self._elimination_error = None
        self._death_positions = {}
        self._death_loot_dropped = set()
        self._death_announced_agents = set()
        self._last_query_note = None
        self._last_hunger_elapsed = 300.0
        self._last_oob_elapsed = 0.0
        self._last_monster_cleanup_elapsed = 0.0
        self._death_objective = "acPvpDeath"
        self._alive_objective = "acPvpAlive"
        self._supply_interval_seconds = 30.0
        self._hunger_after_seconds = 300.0
        self._hunger_interval_seconds = 10.0
        self._hunger_effect_seconds = 10
        self._hunger_effect_amplifier = 3
        self._out_of_bounds_interval_seconds = 10.0
        self._out_of_bounds_damage = 1.0
        self._out_of_bounds_y_padding = 6
        self._monster_cleanup_interval_seconds = 5.0
        self._death_monitor_interval_seconds = 0.2
        self._death_drops_enabled = True
        self._death_drop_tag = "ac_pvp_death"
        self._started_monotonic = None


def _bool_param(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    raise TaskSpecError(f"params.{key} 必须是布尔值")


def _validate_command_name(value: str, label: str) -> None:
    if not value or not _SAFE_NAME_RE.fullmatch(value):
        raise TaskSpecError(f"{label} 包含命令不安全字符")
    if len(value) > 40:
        raise TaskSpecError(f"{label} 过长")


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


def _split_stack_count(count: int) -> list[int]:
    remaining = max(0, int(count))
    result: list[int] = []
    while remaining > 0:
        current = min(64, remaining)
        result.append(current)
        remaining -= current
    return result


def _parse_position(response: Any) -> tuple[float, float, float] | None:
    if response is None:
        return None
    text = str(response)
    if "No entity was found" in text:
        return None
    match = re.search(r"entity data:\s*\[([^\]]+)\]", text)
    if not match:
        return None
    values: list[float] = []
    for item in match.group(1).split(","):
        item = item.strip()
        item = re.sub(r"[dDfF]\s*$", "", item)
        try:
            values.append(float(item))
        except ValueError:
            return None
    if len(values) != 3:
        return None
    return values[0], values[1], values[2]


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


def _format_position_for_message(
    position: tuple[float, float, float] | None,
) -> str:
    if position is None:
        return "未知位置"
    return ", ".join(_format_number(value) for value in position)


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


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


__all__ = ["PVPManager", "PVPTeam", "pvp_team_rosters"]
