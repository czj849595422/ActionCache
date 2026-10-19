"""动态怪物清剿任务管理器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from src.launcher.command_scheduler import (
    BatchCommandScheduler,
    BatchPlan,
    BatchResult,
    parse_batch_file,
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
from src.runtime.liveness import mark_agents_live, set_agent_live_by_name
from src.tasks.monster_clearance import (
    EncounterBounds,
    EncounterSpawnRecord,
    MonsterClearanceGenerationSpec,
    MonsterClearanceScheduleError,
    batch_plans_equal,
    generation_spec_from_template,
    generate_spawn_schedule,
    load_generation_template,
    load_spawn_schedule,
    schedule_to_batch_plan,
)
from src.tasks.spec import TaskSpec, TaskSpecError


SchedulerFactory = Callable[..., BatchCommandScheduler]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
_SCORE_RE = re.compile(r"\bhas(?: the following score)?[: ]+(-?\d+)")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SAFE_ITEM_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_/.-]+$")
_SUPPLY_RESOURCES = {
    "minecraft:iron_sword",
    "minecraft:iron_helmet",
    "minecraft:iron_chestplate",
    "minecraft:iron_leggings",
    "minecraft:iron_boots",
    "minecraft:arrow",
    "minecraft:potion",
    "minecraft:bread",
}
_IRON_ARMOR_PIECES = {
    "minecraft:iron_helmet",
    "minecraft:iron_chestplate",
    "minecraft:iron_leggings",
    "minecraft:iron_boots",
}
_INITIAL_BACKPACK_SLOT_COUNT = 27
_DEFAULT_SUPPLY_INTERVAL_SECONDS = 5.0
_DEFAULT_SUPPLY_MIN_DISTANCE = 10.0
_DEFAULT_SUPPLY_MAX_DISTANCE = 30.0
_DEFAULT_SUPPLY_POLL_INTERVAL = 0.1
_SUPPLY_POSITION_ATTEMPTS = 1000
_SUPPLY_SEED_SALT = 0x5EEDC0DE
_COOPERATIVE_TEAM = "ac_monster_clearance"


@dataclass(frozen=True)
class _SupplyChoice:
    resource: str
    amount: int
    weight: float
    profile: str = "default"
    tag: str | None = None


class MonsterClearanceManager(BaseTaskManager):
    """调度任务怪物和补给，并按唯一实体 tag 判定清场。"""

    def __init__(
        self,
        *,
        scheduler_factory: SchedulerFactory = BatchCommandScheduler,
        monotonic: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
        supply_thread_enabled: bool = True,
        supply_poll_interval: float = _DEFAULT_SUPPLY_POLL_INTERVAL,
    ) -> None:
        if supply_poll_interval <= 0:
            raise ValueError("supply_poll_interval must be positive")
        super().__init__()
        self._scheduler_factory = scheduler_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._supply_thread_enabled = supply_thread_enabled
        self._supply_poll_interval = supply_poll_interval
        self._server: Any = None
        self._agents: list[Any] = []
        self._records: list[EncounterSpawnRecord] = []
        self._monster_records: list[EncounterSpawnRecord] = []
        self._target_monster_count = 0
        self._command_plan = BatchPlan()
        self._batch_records: list[tuple[EncounterSpawnRecord, ...]] = []
        self._next_batch_index = 0
        self._batch_started_at: float | None = None
        self._sent_spawn_ids_runtime: set[str] = set()
        self._spawn_failed_ids: set[str] = set()
        self._bounds: EncounterBounds | None = None
        self._monster_tag = ""
        self._supply_tag = ""
        self._spawn_objective = ""
        self._death_objective = ""
        self._alive_objective = ""
        self._spawned_ids: set[str] = set()
        self._scheduler: BatchCommandScheduler | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_error: Exception | None = None
        self._state_lock = threading.Lock()
        self._terminal_outcome: TaskOutcome | None = None
        self._spawn_results_verified = False
        self._eliminated_agents: set[str] = set()
        self._pending_eliminations: set[str] = set()
        self._elimination_attempts: dict[str, int] = {}
        self._elimination_error: dict[str, str] | None = None
        self._eliminated_monsters: set[str] = set()
        self._monster_health: dict[str, float | None] = {}
        self._last_query_note: str | None = None
        self._generation_seed = 0
        self._supply_items: tuple[_SupplyChoice, ...] = ()
        self._supply_interval_seconds = _DEFAULT_SUPPLY_INTERVAL_SECONDS
        self._supply_min_distance = _DEFAULT_SUPPLY_MIN_DISTANCE
        self._supply_max_distance = _DEFAULT_SUPPLY_MAX_DISTANCE
        self._supply_rng = random.Random()
        self._supply_stop = threading.Event()
        self._supply_thread: threading.Thread | None = None
        self._supply_error: Exception | None = None
        self._next_supply_due: float | None = None
        self._next_supply_number = 1
        self._supply_attempts = 0
        self._supply_drops_sent = 0
        self._supply_skipped_no_agent = 0
        self._supply_skipped_placement = 0
        self._last_supply_drop: dict[str, Any] | None = None

    def configure(self, spec: TaskSpec) -> None:
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            raise TaskSpecError("任务调度仍在运行，请先 teardown")
        if self._supply_thread is not None and self._supply_thread.is_alive():
            raise TaskSpecError("补给掉落线程仍在运行，请先 teardown")
        self._reset_runtime_state()
        super().configure(spec)

    def _validate_spec(self, spec: TaskSpec) -> None:
        if spec.category != "monster_clearance":
            raise TaskSpecError("MonsterClearanceManager 仅支持 monster_clearance")
        names = list(spec.agents.get("names") or [])
        count = int(spec.agents.get("count", 0))
        if (
            count < 1
            or len(names) != count
            or not names
            or len(set(names)) != len(names)
        ):
            raise TaskSpecError(
                "monster_clearance 要求 agents.count >= 1 且 names 唯一并与 count 一致"
            )
        success = spec.completion.get("success") or {}
        if success.get("type") != "all_task_monsters_eliminated":
            raise TaskSpecError(
                "monster_clearance 要求 success.type=all_task_monsters_eliminated"
            )
        failure = spec.completion.get("failure") or {}
        if failure.get("type") != "all_agents_dead":
            raise TaskSpecError("monster_clearance 要求 failure.type=all_agents_dead")
        if spec.spawns:
            raise TaskSpecError("monster_clearance 的动态生成只能来自 schedule_file")
        self._validate_loadout(spec)

        params = spec.params or {}
        required_strings = (
            "monster_tag",
            "supply_tag",
            "spawn_objective",
            "death_objective",
        )
        for key in required_strings:
            value = params.get(key)
            if not isinstance(value, str) or not value:
                raise TaskSpecError(f"params.{key} 必填")
        for key in ("monster_tag", "supply_tag", "spawn_objective", "death_objective"):
            value = str(params[key])
            if not _SAFE_NAME_RE.fullmatch(value):
                raise TaskSpecError(f"params.{key} 包含命令不安全字符")
        if len(str(params["spawn_objective"])) > 16 or len(
            str(params["death_objective"])
        ) > 16:
            raise TaskSpecError("scoreboard objective 名称不能超过 16 个字符")

        try:
            bounds = EncounterBounds.from_dict(params.get("bounds"))
            monster_count = int(params["monster_count"])
            target_monster_count = int(
                params.get("target_monster_count", monster_count)
            )
            initial_count = int(params["initial_monster_count"])
            batch_size = int(params["batch_size"])
            batch_interval = float(params["batch_interval_seconds"])
            last_deadline = float(params["last_spawn_deadline_seconds"])
            monster_distance = float(params["monster_min_distance"])
            initial_agent_distance = float(params["initial_agent_min_distance"])
            supply_interval = float(
                params.get(
                    "supply_interval_seconds",
                    _DEFAULT_SUPPLY_INTERVAL_SECONDS,
                )
            )
            supply_min_distance = float(
                params.get(
                    "supply_drop_min_distance",
                    _DEFAULT_SUPPLY_MIN_DISTANCE,
                )
            )
            supply_max_distance = float(
                params.get(
                    "supply_drop_max_distance",
                    _DEFAULT_SUPPLY_MAX_DISTANCE,
                )
            )
            poll_interval = float(params.get("scheduler_poll_interval", 0.1))
            seed_value = params.get(
                "generation_seed",
                spec.metadata.get("generation_seed", spec.environment.get("seed", 0)),
            )
            agent_positions = tuple(
                (
                    int(block["position"]["x"]),
                    int(block["position"]["y"]),
                    int(block["position"]["z"]),
                )
                for block in spec.initial_state["by_agent"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskSpecError(f"monster_clearance 参数非法: {exc}") from exc
        if not 0 < initial_count <= monster_count:
            raise TaskSpecError("monster_clearance 初始怪物数量非法")
        if not 0 < target_monster_count <= monster_count:
            raise TaskSpecError("monster_clearance 胜利目标数量非法")
        if batch_size < 1 or batch_interval <= 0:
            raise TaskSpecError("monster_clearance 批量刷新参数非法")
        if supply_interval <= 0:
            raise TaskSpecError("monster_clearance 掉落间隔必须大于 0")
        if supply_min_distance < 0 or supply_max_distance < supply_min_distance:
            raise TaskSpecError(
                "params.supply_drop_min_distance/max_distance 必须满足 0 <= min <= max"
            )
        if poll_interval <= 0:
            raise TaskSpecError("params.scheduler_poll_interval 必须大于 0")
        if not isinstance(seed_value, int):
            raise TaskSpecError("params.generation_seed 必须是整数")
        supply_items = _parse_supply_choices(params.get("supply_weights"))
        schedule_value = params.get("schedule_file")
        command_value = params.get("command_file")
        runtime_mode = schedule_value is None and command_value is None
        if (schedule_value is None) != (command_value is None):
            raise TaskSpecError(
                "params.schedule_file 和 params.command_file 必须同时存在或同时省略"
            )
        command_options = {
            "monster_tag": str(params["monster_tag"]),
            "supply_tag": str(params["supply_tag"]),
            "spawn_objective": str(params["spawn_objective"]),
        }
        try:
            if runtime_mode:
                template_value = params.get("generation_template_file")
                if not isinstance(template_value, str) or not template_value:
                    raise TaskSpecError(
                        "monster_clearance 需要 params.generation_template_file"
                    )
                template = load_generation_template(Path(template_value))
                template_generation_spec = generation_spec_from_template(template)
                generation_spec = replace(
                    template_generation_spec,
                    bounds=bounds,
                    monster_count=monster_count,
                    initial_monster_count=initial_count,
                    batch_interval_seconds=int(batch_interval),
                    last_spawn_deadline_seconds=int(last_deadline),
                    monster_min_distance=float(monster_distance),
                    agent_positions=agent_positions,
                    initial_agent_min_distance=float(initial_agent_distance),
                    supply_drop_min_distance=float(supply_min_distance),
                    supply_drop_max_distance=float(supply_max_distance),
                    supply_weights=tuple(
                        (item.resource, item.weight) for item in supply_items
                    ),
                    supply_amounts=tuple(
                        (item.resource, item.amount) for item in supply_items
                    ),
                    batch_size=batch_size,
                    supply_interval_seconds=int(supply_interval),
                    agent_spawn_center=(
                        float(spec.environment.get("world_spawn", {}).get("x", 0)),
                        float(spec.environment.get("world_spawn", {}).get("y", 0)),
                        float(spec.environment.get("world_spawn", {}).get("z", 0)),
                    ),
                )
                records = generate_spawn_schedule(generation_spec, seed_value)
                monster_records = [
                    record for record in records if record.kind == "monster"
                ]
                command_plan = schedule_to_batch_plan(
                    monster_records, **command_options
                )
                expected_plan = command_plan
            else:
                schedule_path = spec.resolve_resource(str(schedule_value))
                command_path = spec.resolve_resource(str(command_value))
                records = load_spawn_schedule(
                    schedule_path,
                    bounds=bounds,
                    timeout_seconds=float(spec.completion["timeout_seconds"]),
                    expected_monster_count=monster_count,
                    initial_monster_count=initial_count,
                    last_spawn_deadline_seconds=last_deadline,
                    monster_min_distance=monster_distance,
                    agent_positions=agent_positions,
                    initial_agent_min_distance=initial_agent_distance,
                    batch_interval_seconds=batch_interval,
                )
                monster_records = [
                    record for record in records if record.kind == "monster"
                ]
                expected_plan = schedule_to_batch_plan(
                    monster_records, **command_options
                )
                command_plan = _monster_only_plan(parse_batch_file(command_path))
        except (MonsterClearanceScheduleError, OSError, ValueError) as exc:
            raise TaskSpecError(
                f"无法加载 monster_clearance 调度资源: {exc}"
            ) from exc
        if not batch_plans_equal(expected_plan, command_plan):
            raise TaskSpecError("params.command_file 与 schedule_file 转换结果不一致")

        self._records = monster_records
        self._monster_records = monster_records
        self._target_monster_count = target_monster_count
        self._command_plan = command_plan
        self._batch_records = _split_monster_batches(
            monster_records,
            initial_count=initial_count,
            batch_size=batch_size,
        )
        self._bounds = bounds
        self._monster_tag = str(params["monster_tag"])
        self._supply_tag = str(params["supply_tag"])
        self._spawn_objective = str(params["spawn_objective"])
        self._death_objective = str(params["death_objective"])
        self._alive_objective = "acAlive"
        self._generation_seed = seed_value
        self._supply_items = supply_items
        self._supply_interval_seconds = supply_interval
        self._supply_min_distance = supply_min_distance
        self._supply_max_distance = supply_max_distance

    def _validate_loadout(self, spec: TaskSpec) -> None:
        state = spec.initial_state
        shared = state.get("per_agent") or {}
        inventory = shared.get("inventory") or {}
        base_inventory = {
            "minecraft:stone_sword": 1,
            "minecraft:bow": 1,
            "minecraft:arrow": 16,
        }
        extra = set(inventory) - set(base_inventory)
        if not base_inventory.items() <= inventory.items():
            raise TaskSpecError(
                "monster_clearance 初始武器、弓和箭配置不正确"
            )
        if len(extra) != 2 or not extra <= _IRON_ARMOR_PIECES:
            raise TaskSpecError(
                "monster_clearance 初始铁防具必须为恰好 2 件且仅含铁防具"
            )
        if any(
            inventory.get(item, 0) != 1 for item in extra
        ):
            raise TaskSpecError("monster_clearance 初始铁防具每件数量必须为 1")
        if shared.get("equipment"):
            raise TaskSpecError(
                "monster_clearance 初始装备必须放入背包，不得直接穿戴"
            )
        positions = state.get("by_agent") or []
        expected_names = list((self.spec.agents.get("names") or []) if self.spec else [])
        if [block.get("name") for block in positions] != expected_names or any(
            "position" not in block for block in positions
        ):
            raise TaskSpecError(
                "monster_clearance 必须为每名 Agent 分别配置出生点"
            )

    def _initial_state_commands(self, names: list[str]) -> list[str]:
        """清剿任务初始物资写入背包主格，避免登录后默认手持武器。"""
        state = self.spec.initial_state if self.spec else {}
        commands: list[str] = []
        per_agent = state.get("per_agent") or {}
        for name in names:
            commands.extend(
                _backpack_inventory_commands(
                    name,
                    per_agent.get("inventory") or {},
                    label="initial_state.per_agent.inventory",
                )
            )
            position = per_agent.get("position")
            if position:
                commands.append(
                    f"tp {name} {position['x']} {position['y']} {position['z']}"
                )
        for override in state.get("by_agent") or []:
            name = str(override["name"])
            commands.extend(
                _backpack_inventory_commands(
                    name,
                    override.get("inventory") or {},
                    label=f"initial_state.by_agent[{name}].inventory",
                )
            )
            position = override.get("position")
            if position:
                commands.append(
                    f"tp {name} {position['x']} {position['y']} {position['z']}"
                )
        return commands

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        actual_names = self._agent_names(agents)
        expected_names = self._agent_names([])
        if actual_names != expected_names:
            raise TaskSpecError(
                f"实际 Agent 必须与配置顺序一致: expected={expected_names}, "
                f"actual={actual_names}"
            )
        self._server = server
        self._agents = list(agents)
        mark_agents_live(self._agents, True)
        self._prepare_scoreboards_and_tags(server, expected_names)
        super().prepare(server, agents, memory)
        self._apply_commands(
            server,
            self._cooperative_team_commands(_COOPERATIVE_TEAM, expected_names),
        )

    def _prepare_scoreboards_and_tags(
        self,
        server: Any,
        names: list[str],
    ) -> None:
        commands = [
            *self._clear_field_commands(),
            f"kill @e[tag={self._monster_tag}]",
            f"kill @e[tag={self._supply_tag}]",
            f"scoreboard objectives remove {self._spawn_objective}",
            f"scoreboard objectives add {self._spawn_objective} dummy",
            f"scoreboard objectives remove {self._death_objective}",
            f"scoreboard objectives add {self._death_objective} deathCount",
            f"scoreboard objectives remove {self._alive_objective}",
            f"scoreboard objectives add {self._alive_objective} dummy",
        ]
        commands.extend(
            f"scoreboard players set {name} {self._death_objective} 0"
            for name in names
        )
        self._apply_commands(server, commands)

    def _clear_field_commands(self) -> list[str]:
        """清理场地内全部非玩家实体。

        先 forceload 整个场地区块，确保基准存档预存的实体（含尚未加载区块
        中的 mob）被加载后再统一清除，随后解除强制加载。
        """
        if self._bounds is None:
            return []
        bounds = self._bounds
        return [
            f"forceload add "
            f"{bounds.min_x // 16} {bounds.min_z // 16} "
            f"{bounds.max_x // 16} {bounds.max_z // 16}",
            f"kill @e[type=!player,"
            f"x={bounds.min_x},y={bounds.min_y},z={bounds.min_z},"
            f"dx={bounds.max_x - bounds.min_x},"
            f"dy={bounds.max_y - bounds.min_y + 2},"
            f"dz={bounds.max_z - bounds.min_z}]",
            "forceload remove all",
        ]

    def start(self) -> None:
        super().start()
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        self._launch_next_batch()
        self._start_supply_runtime()

    def _launch_next_batch(self) -> bool:
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        if self._next_batch_index >= len(self._batch_records):
            return False
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            return False
        batch = self._batch_records[self._next_batch_index]
        batch_ids = {record.spawn_id for record in batch}
        entries = [
            replace(entry, time=0.0, unit="s")
            for entry in self._command_plan.task
            if _entry_spawn_id(entry.command) in batch_ids
        ]
        if len(entries) != len(batch):
            raise TaskSpecError("monster_clearance 批次命令与生成记录数量不一致")
        poll_interval = float(
            (self.spec.params if self.spec else {}).get(
                "scheduler_poll_interval", 0.1
            )
        )
        scheduler = self._scheduler_factory(
            self._server,
            BatchPlan(task=entries),
            poll_interval=poll_interval,
        )
        self._scheduler = scheduler
        self._next_batch_index += 1
        self._batch_started_at = self._monotonic()
        self._scheduler_thread = threading.Thread(
            target=self._run_scheduler,
            args=(scheduler,),
            name=f"monster-clearance-{self.spec.task_id if self.spec else 'task'}",
            daemon=True,
        )
        self._scheduler_thread.start()
        return True

    def _run_scheduler(self, scheduler: BatchCommandScheduler) -> None:
        try:
            scheduler.run_task()
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._scheduler_error = exc

    def _start_supply_runtime(self) -> None:
        with self._state_lock:
            self._supply_stop.clear()
            self._supply_rng = random.Random(self._generation_seed ^ _SUPPLY_SEED_SALT)
            self._supply_error = None
            self._next_supply_due = self._monotonic()
            self._next_supply_number = 1
            self._supply_attempts = 0
            self._supply_drops_sent = 0
            self._supply_skipped_no_agent = 0
            self._supply_skipped_placement = 0
            self._last_supply_drop = None
        if not self._supply_thread_enabled:
            return
        self._supply_thread = threading.Thread(
            target=self._run_supply_loop,
            name=f"monster-supply-{self.spec.task_id if self.spec else 'task'}",
            daemon=True,
        )
        self._supply_thread.start()

    def _run_supply_loop(self) -> None:
        try:
            while not self._supply_stop.is_set():
                now = self._monotonic()
                self._run_supply_step(now)
                wait_seconds = self._next_supply_wait_seconds()
                if wait_seconds > 0:
                    self._sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._supply_error = exc
            self._supply_stop.set()

    def _next_supply_wait_seconds(self) -> float:
        with self._state_lock:
            due = self._next_supply_due
        if due is None:
            return self._supply_poll_interval
        return max(0.0, min(self._supply_poll_interval, due - self._monotonic()))

    def _run_supply_step(self, now: float | None = None) -> None:
        now = self._monotonic() if now is None else now
        while not self._supply_stop.is_set():
            with self._state_lock:
                due = self._next_supply_due
            if due is None or now < due:
                return
            with self._state_lock:
                self._supply_attempts += 1
            try:
                self._drop_supply_once()
            except Exception as exc:  # noqa: BLE001
                with self._state_lock:
                    self._supply_error = exc
                self._supply_stop.set()
                return
            with self._state_lock:
                self._next_supply_due = due + self._supply_interval_seconds

    def _drop_supply_once(self) -> bool:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        candidates = self._alive_agent_positions()
        if not candidates:
            with self._state_lock:
                self._supply_skipped_no_agent += 1
            return False
        drop: tuple[str, tuple[float, float, float]] | None = None
        for _ in range(_SUPPLY_POSITION_ATTEMPTS):
            agent_name, origin = self._supply_rng.choice(candidates)
            position = self._random_supply_position(origin)
            if position is not None:
                drop = (agent_name, position)
                break
        if drop is None:
            with self._state_lock:
                self._supply_skipped_placement += 1
            return False
        agent_name, drop_position = drop
        item = _weighted_supply_choice(self._supply_rng, self._supply_items)
        with self._state_lock:
            supply_number = self._next_supply_number
            self._next_supply_number += 1
        command = _supply_summon_command(
            item,
            drop_position,
            supply_tag=self._supply_tag,
            record_tag=f"ac_supply_{supply_number:04d}",
        )
        response = self._server.send_command(command)
        with self._state_lock:
            self._supply_drops_sent += 1
            self._last_supply_drop = {
                "agent": agent_name,
                "item": item.resource,
                "amount": item.amount,
                "position": {
                    "x": drop_position[0],
                    "y": drop_position[1],
                    "z": drop_position[2],
                },
                "response": str(response),
            }
        return True

    def _alive_agent_positions(self) -> list[tuple[str, tuple[float, float, float]]]:
        server = self._server
        candidates: list[tuple[str, tuple[float, float, float]]] = []
        agent_by_name = {
            str(getattr(agent, "username", agent)): agent for agent in self._agents
        }
        for name in self._agent_names([]):
            with self._state_lock:
                eliminated = name in self._eliminated_agents
            if eliminated:
                continue
            agent = agent_by_name.get(name)
            snapshot = _agent_raw_snapshot(agent) if agent is not None else None
            if snapshot is not None:
                if _snapshot_is_alive(snapshot) is False:
                    continue
                position = _snapshot_position(snapshot)
                if position is not None:
                    candidates.append((name, position))
                    continue
            if server is None:
                continue
            death_score = self._query_score(
                server,
                name,
                self._death_objective,
                priority=True,
            )
            if death_score is not None and death_score > 0:
                continue
            health = _query_entity_health(server, name)
            if health is None or health <= 0:
                continue
            position = _query_entity_position(server, name)
            if position is not None:
                candidates.append((name, position))
        return candidates

    def _random_supply_position(
        self,
        origin: tuple[float, float, float],
    ) -> tuple[float, float, float] | None:
        bounds = self._bounds
        if bounds is None:
            raise TaskSpecError("monster_clearance 尚未配置 bounds")
        for _ in range(_SUPPLY_POSITION_ATTEMPTS):
            distance = self._supply_rng.uniform(
                self._supply_min_distance,
                self._supply_max_distance,
            )
            angle = self._supply_rng.uniform(0.0, math.tau)
            x = _round_coordinate(origin[0] + math.cos(angle) * distance)
            z = _round_coordinate(origin[2] + math.sin(angle) * distance)
            y = float(bounds.min_y)
            actual_distance = math.hypot(x - origin[0], z - origin[2])
            if actual_distance < self._supply_min_distance:
                continue
            if actual_distance > self._supply_max_distance:
                continue
            if not _position_within_bounds(bounds, (x, y, z)):
                continue
            return (x, y, z)
        return None

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if not self._started:
            raise TaskSpecError("任务管理器尚未 start")
        self._last_query_note = None
        with self._state_lock:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
            scheduler_error = self._scheduler_error
            supply_error = self._supply_error
        snapshot = self._scheduler.result_snapshot() if self._scheduler else None
        self._collect_sent_spawn_ids(snapshot)
        if (
            scheduler_error is not None
            or supply_error is not None
            or (
                snapshot is not None and (snapshot.errors or snapshot.prepare_errors)
            )
        ):
            detail: dict[str, Any] = {}
            if scheduler_error is not None:
                detail["error"] = str(scheduler_error)
            if supply_error is not None:
                detail["supply_error"] = str(supply_error)
            if snapshot is not None and snapshot.errors:
                detail["command_errors"] = [
                    {"command": entry.command, "error": error}
                    for entry, error in snapshot.errors
                ]
            return self._latch(
                TaskOutcome(TaskStatus.FAILURE, reason="schedule_failed", detail=detail)
            )

        agent_query_ok = self._update_agent_eliminations(context)
        if self._elimination_error is not None:
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="agent_elimination_failed",
                    detail=dict(self._elimination_error),
                )
            )
        if len(self._eliminated_agents) == len(self._agent_names([])):
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="all_agents_dead",
                    detail=self._status_metrics(context, snapshot),
                )
            )

        monster_query_ok = self._update_monster_aliveness(context.server)
        if self._spawn_failed_ids:
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="spawn_failed",
                    detail=self._status_metrics(context, snapshot),
                )
            )

        try:
            self._launch_next_batch_if_ready()
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._scheduler_error = exc
            return self._latch(
                TaskOutcome(
                    TaskStatus.FAILURE,
                    reason="schedule_failed",
                    detail={"error": str(exc)},
                )
            )

        schedule_complete = self._all_batches_dispatched()
        if schedule_complete and not self._spawn_results_verified:
            verification = self._verify_spawn_results(context.server)
            if verification is False:
                return self._latch(
                    TaskOutcome(
                        TaskStatus.FAILURE,
                        reason="spawn_failed",
                        detail=self._status_metrics(context, snapshot),
                    )
                )
            if verification is True:
                self._spawn_results_verified = True

        alive_agents = self._alive_agents(context) if agent_query_ok else []
        if (
            self._spawn_results_verified
            and monster_query_ok
            and alive_agents
            and len(self._eliminated_monsters) >= self._target_monster_count
        ):
            detail = self._status_metrics(context, snapshot)
            try:
                send_priority_command(
                    context.server,
                    f"kill @e[tag={self._monster_tag}]",
                )
            except Exception as exc:  # noqa: BLE001
                detail["cleanup_error"] = str(exc)
            return self._latch(
                TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="all_task_monsters_eliminated",
                    detail=detail,
                )
            )
        if context.elapsed_seconds() >= float(
            self.spec.completion["timeout_seconds"]
        ):
            return self._latch(
                TaskOutcome(
                    TaskStatus.TIMEOUT,
                    reason="timeout",
                    detail=self._status_metrics(context, snapshot),
                )
            )
        return TaskOutcome(
            TaskStatus.RUNNING,
            detail=self._status_metrics(context, snapshot),
        )

    def _collect_sent_spawn_ids(self, snapshot: BatchResult | None) -> None:
        if snapshot is None:
            return
        self._sent_spawn_ids_runtime.update(
            _sent_spawn_ids(snapshot)
        )

    def _all_batches_dispatched(self) -> bool:
        return (
            self._next_batch_index >= len(self._batch_records)
            and len(self._sent_spawn_ids_runtime) == len(self._monster_records)
        )

    def _launch_next_batch_if_ready(self) -> None:
        if self._next_batch_index >= len(self._batch_records):
            return
        snapshot = self._scheduler.result_snapshot() if self._scheduler else None
        if snapshot is None or snapshot.errors or snapshot.cancelled:
            return
        batch = self._batch_records[self._next_batch_index - 1]
        batch_ids = {record.spawn_id for record in batch}
        if not batch_ids <= self._sent_spawn_ids_runtime:
            return
        interval = float(
            (self.spec.params if self.spec else {}).get(
                "batch_interval_seconds", 30
            )
        )
        interval_elapsed = (
            self._batch_started_at is not None
            and self._monotonic() - self._batch_started_at >= interval
        )
        if not interval_elapsed:
            return
        self._launch_next_batch()

    def _update_agent_eliminations(self, context: EvaluatorContext) -> bool:
        server = context.server
        query_ok = True
        for name in self._agent_names([]):
            if name in self._eliminated_agents:
                continue
            score = self._query_score(
                server,
                name,
                self._death_objective,
                priority=True,
            )
            if score is None:
                query_ok = False
                self._last_query_note = f"无法读取 {name} 死亡计分"
            snapshot = _context_snapshot(context, name)
            game_mode = snapshot_game_mode(snapshot)
            dead = game_mode == "spectator" if game_mode is not None else None
            if snapshot is None:
                self._last_query_note = (
                    f"无法读取 {name} 感知状态"
                    if score is not None
                    else f"无法读取 {name} 死亡计分和感知状态"
                )
            if name in self._pending_eliminations:
                dead = True
            if score is None and dead is None:
                continue
            if not death_score_indicates_elimination(score) and dead is not True:
                continue
            set_agent_live_by_name(self._agents, name, False)
            self._pending_eliminations.add(name)
            success, error = self._set_agent_spectator(context, name, snapshot)
            if not success:
                attempts = self._elimination_attempts.get(name, 0) + 1
                self._elimination_attempts[name] = attempts
                self._last_query_note = f"无法淘汰 {name}: {error}"
                if attempts >= self.max_elimination_attempts:
                    self._elimination_error = {
                        "agent": name,
                        "error": error or "无法确认旁观者模式",
                        "attempts": str(attempts),
                    }
                continue
            self._pending_eliminations.discard(name)
            self._elimination_attempts.pop(name, None)
            self._eliminated_agents.add(name)
            try:
                context.server.send_command(
                    f"scoreboard players set {name} {self._alive_objective} 0"
                )
            except Exception as exc:  # noqa: BLE001
                self._last_query_note = f"无法更新 {name} 死亡状态: {exc}"
        return query_ok

    def _set_agent_spectator(
        self,
        context: EvaluatorContext,
        name: str,
        snapshot: Mapping[str, Any] | None,
    ) -> tuple[bool, str | None]:
        try:
            response = send_priority_command(
                context.server,
                spectator_score_selector(name, self._death_objective)
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return spectator_command_verified(response, _context_snapshot(context, name) or snapshot)

    def _verify_spawn_results(self, server: Any) -> bool | None:
        for record in self._monster_records:
            score = self._query_score(
                server,
                f"#{record.spawn_id}",
                self._spawn_objective,
            )
            if score is None:
                self._last_query_note = f"无法读取 {record.spawn_id} 召唤结果"
                return None
            if score != 1:
                self._last_query_note = f"{record.spawn_id} 召唤失败"
                return False
        return True

    def _update_monster_aliveness(self, server: Any) -> bool:
        """按 spawn 状态增量跟踪怪物存活：实体存在即存活，消失即已消灭。"""
        query_ok = True
        for record in self._monster_records:
            if record.spawn_id in self._eliminated_monsters:
                continue
            if record.spawn_id not in self._sent_spawn_ids_runtime:
                continue
            if record.spawn_id not in self._spawned_ids:
                spawn_score = self._query_score(
                    server,
                    f"#{record.spawn_id}",
                    self._spawn_objective,
                )
                if spawn_score is None:
                    query_ok = False
                    self._last_query_note = (
                        f"无法解析 {record.spawn_id} 的召唤结果"
                    )
                    continue
                if spawn_score == 0:
                    # 尚未召唤成功，不能视为已消灭。
                    if record.spawn_id in self._sent_spawn_ids_runtime:
                        self._spawn_failed_ids.add(record.spawn_id)
                    continue
                self._spawned_ids.add(record.spawn_id)
            try:
                server.send_command(
                    f"execute store result score #{record.spawn_id} "
                    f"{self._alive_objective} if entity "
                    f"@e[tag=ac_spawn_{record.spawn_id},limit=1]"
                )
                alive = self._query_score(
                    server,
                    f"#{record.spawn_id}",
                    self._alive_objective,
                )
            except Exception as exc:  # noqa: BLE001
                query_ok = False
                self._last_query_note = f"无法查询 {record.spawn_id} 存活状态: {exc}"
                continue
            if alive is None:
                query_ok = False
                self._monster_health[record.spawn_id] = None
                self._last_query_note = f"无法解析 {record.spawn_id} 的存活查询"
            elif alive <= 0:
                self._monster_health[record.spawn_id] = 0.0
                self._eliminated_monsters.add(record.spawn_id)
            else:
                self._monster_health[record.spawn_id] = 1.0
        return query_ok

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

    def _alive_agents(self, context: EvaluatorContext) -> list[str]:
        alive: list[str] = []
        for name in self._agent_names([]):
            if name in self._eliminated_agents:
                continue
            health = context.health(name)
            if health is not None and health > 0:
                alive.append(name)
        return alive

    def _latch(self, outcome: TaskOutcome) -> TaskOutcome:
        should_stop_supply = False
        with self._state_lock:
            if self._terminal_outcome is None:
                self._terminal_outcome = outcome
                should_stop_supply = True
            latched = self._terminal_outcome
        if should_stop_supply:
            self._stop_supply_runtime()
        return latched

    def _status_metrics(
        self,
        context: EvaluatorContext,
        snapshot: BatchResult | None,
    ) -> dict[str, Any]:
        self._collect_sent_spawn_ids(snapshot)
        sent_ids = set(self._sent_spawn_ids_runtime)
        monsters_by_type: dict[str, dict[str, int]] = {}
        monsters_by_profile: dict[str, dict[str, int]] = {}
        for record in self._monster_records:
            resource = record.resource.split(":", 1)[-1]
            for target, key in (
                (monsters_by_type, resource),
                (monsters_by_profile, record.profile),
            ):
                counts = target.setdefault(key, {"planned": 0, "spawned": 0, "alive": 0})
                counts["planned"] += 1
                if record.spawn_id in sent_ids:
                    counts["spawned"] += 1
                if (
                    record.spawn_id in sent_ids
                    and record.spawn_id not in self._eliminated_monsters
                ):
                    counts["alive"] += 1
        spawned_monsters = sum(
            record.spawn_id in sent_ids for record in self._monster_records
        )
        alive_monsters = sum(
            record.spawn_id in sent_ids
            and record.spawn_id not in self._eliminated_monsters
            for record in self._monster_records
        )
        with self._state_lock:
            supply_error = self._supply_error
            last_supply_drop = (
                dict(self._last_supply_drop) if self._last_supply_drop else None
            )
            supply_thread = self._supply_thread
            next_supply_due = self._next_supply_due
            supply_counters = {
                "supply_attempts": self._supply_attempts,
                "spawned_supplies": self._supply_drops_sent,
                "supply_skipped_no_alive_agent": self._supply_skipped_no_agent,
                "supply_skipped_placement": self._supply_skipped_placement,
            }
        detail: dict[str, Any] = {
            "elapsed_seconds": max(0.0, float(context.elapsed_seconds())),
            "planned_monsters": len(self._monster_records),
            "target_monsters": self._target_monster_count,
            "spawned_monsters": spawned_monsters,
            "alive_monsters": alive_monsters,
            "eliminated_monsters": len(self._eliminated_monsters),
            "monsters_by_type": monsters_by_type,
            "monsters_by_profile": monsters_by_profile,
            **supply_counters,
            "last_supply_drop": last_supply_drop,
            "supply_error": str(supply_error) if supply_error else None,
            "supply_thread_alive": bool(supply_thread and supply_thread.is_alive()),
            "next_supply_drop_in_seconds": (
                _number(max(0.0, next_supply_due - self._monotonic()))
                if next_supply_due is not None
                else None
            ),
            "alive_agents": [
                name
                for name in self._agent_names([])
                if name not in self._eliminated_agents
            ],
            "eliminated_agents": sorted(self._eliminated_agents),
            "pending_eliminations": sorted(self._pending_eliminations),
            "elimination_error": (
                dict(self._elimination_error) if self._elimination_error else None
            ),
            "commands_sent": len(sent_ids),
            "command_errors": len(snapshot.errors) if snapshot else 0,
            "schedule_complete": self._all_batches_dispatched(),
            "spawn_results_verified": self._spawn_results_verified,
        }
        if self._last_query_note:
            detail["query_note"] = self._last_query_note
        return detail

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        snapshot = self._scheduler.result_snapshot() if self._scheduler else None
        return self._status_metrics(context, snapshot)

    def teardown(self) -> None:
        self._stop_supply_runtime()
        scheduler = self._scheduler
        thread = self._scheduler_thread
        server = self._server
        names = self._agent_names([])
        bounds = self._bounds
        if scheduler is not None:
            scheduler.cancel()
        if thread is not None and thread.is_alive():
            thread.join(timeout=12.0)
        if server is not None:
            commands = [
                f"kill @e[tag={self._monster_tag}]",
                f"kill @e[tag={self._supply_tag}]",
            ]
            if bounds is not None:
                commands.append(
                    f"kill @e[type=item,x={bounds.min_x},y={bounds.min_y},"
                    f"z={bounds.min_z},dx={bounds.max_x - bounds.min_x},"
                    f"dy={bounds.max_y - bounds.min_y + 2},"
                    f"dz={bounds.max_z - bounds.min_z}]"
                )
            commands.extend(f"gamemode survival {name}" for name in names)
            commands.extend(
                (
                    f"scoreboard objectives remove {self._spawn_objective}",
                    f"scoreboard objectives remove {self._death_objective}",
                    f"scoreboard objectives remove {self._alive_objective}",
                )
            )
            commands.append(f"team remove {_COOPERATIVE_TEAM}")
            for command in commands:
                try:
                    server.send_command(command)
                except Exception:  # noqa: BLE001
                    pass
        self._reset_runtime_state()
        super().teardown()

    def _stop_supply_runtime(self) -> None:
        thread = self._supply_thread
        self._supply_stop.set()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=12.0)

    def _reset_runtime_state(self) -> None:
        self._server = None
        self._agents = []
        self._records = []
        self._monster_records = []
        self._target_monster_count = 0
        self._command_plan = BatchPlan()
        self._batch_records = []
        self._next_batch_index = 0
        self._batch_started_at = None
        self._sent_spawn_ids_runtime = set()
        self._spawn_failed_ids = set()
        self._bounds = None
        self._monster_tag = ""
        self._supply_tag = ""
        self._spawn_objective = ""
        self._death_objective = ""
        self._alive_objective = ""
        self._spawned_ids = set()
        self._scheduler = None
        self._scheduler_thread = None
        self._scheduler_error = None
        self._terminal_outcome = None
        self._spawn_results_verified = False
        self._eliminated_agents = set()
        self._pending_eliminations = set()
        self._elimination_attempts = {}
        self._elimination_error = None
        self._eliminated_monsters = set()
        self._monster_health = {}
        self._last_query_note = None
        self._generation_seed = 0
        self._supply_items = ()
        self._supply_interval_seconds = _DEFAULT_SUPPLY_INTERVAL_SECONDS
        self._supply_min_distance = _DEFAULT_SUPPLY_MIN_DISTANCE
        self._supply_max_distance = _DEFAULT_SUPPLY_MAX_DISTANCE
        self._supply_rng = random.Random()
        self._supply_stop = threading.Event()
        self._supply_thread = None
        self._supply_error = None
        self._next_supply_due = None
        self._next_supply_number = 1
        self._supply_attempts = 0
        self._supply_drops_sent = 0
        self._supply_skipped_no_agent = 0
        self._supply_skipped_placement = 0
        self._last_supply_drop = None


def _backpack_inventory_commands(
    name: str,
    inventory: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    if not inventory:
        return []
    if len(inventory) > _INITIAL_BACKPACK_SLOT_COUNT:
        raise TaskSpecError(
            f"{label} 物品种类超过背包主格数量 {_INITIAL_BACKPACK_SLOT_COUNT}"
        )
    commands: list[str] = []
    for slot, (item, count) in enumerate(inventory.items()):
        commands.append(
            f"item replace entity {name} inventory.{slot} with {item} {int(count)}"
        )
    return commands


def _sent_spawn_ids(snapshot: BatchResult | None) -> set[str]:
    if snapshot is None:
        return set()
    sent: set[str] = set()
    for entry, _ in snapshot.sent:
        match = re.search(r"score #(m|i)(\d{4})\s", entry.command)
        if match:
            sent.add(f"{match.group(1)}{match.group(2)}")
    return sent


def _entry_spawn_id(command: str) -> str | None:
    match = re.search(r"score #(m\d{4})\s", command)
    return match.group(1) if match else None


def _split_monster_batches(
    records: Sequence[EncounterSpawnRecord],
    *,
    initial_count: int,
    batch_size: int,
) -> list[tuple[EncounterSpawnRecord, ...]]:
    ordered = sorted(
        records,
        key=lambda record: (record.spawn_time_s, record.line_no, record.spawn_id),
    )
    batches = [tuple(ordered[:initial_count])]
    batches.extend(
        tuple(ordered[index : index + batch_size])
        for index in range(initial_count, len(ordered), batch_size)
    )
    return [batch for batch in batches if batch]


def _monster_only_plan(plan: BatchPlan) -> BatchPlan:
    return BatchPlan(
        prepare=list(plan.prepare),
        task=[
            entry
            for entry in plan.task
            if re.search(r"score #m\d{4}\s", entry.command)
        ],
    )


def _parse_supply_choices(value: Any) -> tuple[_SupplyChoice, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskSpecError("params.supply_weights 必须是对象列表")
    choices: list[_SupplyChoice] = []
    expected_amounts = {
        "minecraft:arrow": 8,
        "minecraft:bread": 2,
    }
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise TaskSpecError(f"params.supply_weights[{index}] 必须是映射")
        resource = _minecraft_item_id(raw.get("resource") or raw.get("item"))
        if resource is None:
            raise TaskSpecError(f"params.supply_weights[{index}].resource 必填")
        if resource not in _SUPPLY_RESOURCES:
            raise TaskSpecError(f"掉落物不在允许池内: {resource}")
        try:
            amount = int(raw.get("amount", expected_amounts.get(resource, 1)))
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise TaskSpecError(
                f"params.supply_weights[{index}] amount/weight 非法"
            ) from exc
        if amount != expected_amounts.get(resource, 1):
            raise TaskSpecError(
                f"{resource} 掉落数量必须为 {expected_amounts.get(resource, 1)}"
            )
        if weight <= 0:
            raise TaskSpecError(
                f"params.supply_weights[{index}].weight 必须大于 0"
            )
        profile = str(raw.get("profile") or (
            "healing" if resource == "minecraft:potion" else "default"
        ))
        tag = raw.get("tag")
        if tag is not None and not isinstance(tag, str):
            raise TaskSpecError(f"params.supply_weights[{index}].tag 必须是字符串")
        choices.append(
            _SupplyChoice(
                resource=resource,
                amount=amount,
                weight=weight,
                profile=profile,
                tag=tag,
            )
        )
    if not choices:
        raise TaskSpecError("params.supply_weights 不能为空")
    return tuple(choices)


def _weighted_supply_choice(
    rng: random.Random,
    choices: Sequence[_SupplyChoice],
) -> _SupplyChoice:
    total = sum(choice.weight for choice in choices)
    cursor = rng.uniform(0.0, total)
    upto = 0.0
    for choice in choices:
        upto += choice.weight
        if cursor <= upto:
            return choice
    return choices[-1]


def _context_snapshot(
    context: EvaluatorContext,
    name: str,
) -> dict[str, Any] | None:
    snapshot_method = getattr(context, "_snapshot", None)
    if callable(snapshot_method):
        try:
            snapshot = snapshot_method(name)
        except Exception:  # noqa: BLE001
            return None
        return snapshot if isinstance(snapshot, dict) else None
    return None


def _agent_raw_snapshot(agent: Any) -> dict[str, Any] | None:
    raw = getattr(agent, "get_raw_perception", None)
    if not callable(raw):
        return None
    try:
        snapshot = raw()
    except Exception:  # noqa: BLE001
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _snapshot_is_alive(snapshot: Mapping[str, Any]) -> bool | None:
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, Mapping):
            continue
        game_mode = candidate.get("gameMode")
        if game_mode is None:
            game_mode = candidate.get("gamemode")
        if isinstance(game_mode, str) and game_mode.strip().lower() == "spectator":
            return False
        health = candidate.get("health")
        if isinstance(health, (int, float)):
            return float(health) > 0
    return None


def _snapshot_position(snapshot: Mapping[str, Any]) -> tuple[float, float, float] | None:
    for candidate in (snapshot.get("self"), snapshot):
        if not isinstance(candidate, Mapping):
            continue
        position = _position_tuple(candidate.get("position"))
        if position is not None:
            return position
    return None


def _query_entity_health(server: Any, target: str) -> float | None:
    try:
        response = server.send_command(f"data get entity {target} Health")
    except Exception:  # noqa: BLE001
        return None
    return _parse_health_value(response)


def _query_entity_position(server: Any, target: str) -> tuple[float, float, float] | None:
    try:
        response = server.send_command(f"data get entity {target} Pos")
    except Exception:  # noqa: BLE001
        return None
    return _parse_position(response)


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


def _parse_position(response: Any) -> tuple[float, float, float] | None:
    if response is None:
        return None
    text = str(response)
    if "No entity was found" in text:
        return None
    match = re.search(r"entity data:\s*\[([^\]]+)\]", text)
    if not match:
        return None
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) != 3:
        return None
    values: list[float] = []
    for part in parts:
        part = re.sub(r"[dDfF]\s*$", "", part)
        try:
            values.append(float(part))
        except ValueError:
            return None
    return values[0], values[1], values[2]


def _position_tuple(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return float(value["x"]), float(value["y"]), float(value["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _position_within_bounds(
    bounds: EncounterBounds,
    position: tuple[float, float, float],
) -> bool:
    x, y, z = position
    return (
        bounds.min_x <= x <= bounds.max_x
        and bounds.min_y <= y <= bounds.max_y
        and bounds.min_z <= z <= bounds.max_z
    )


def _supply_summon_command(
    item: _SupplyChoice,
    position: tuple[float, float, float],
    *,
    supply_tag: str,
    record_tag: str,
) -> str:
    x, y, z = (_format_coordinate(value) for value in position)
    item_tag = _supply_item_tag(item)
    item_nbt = (
        f'Item:{{id:"{item.resource}",Count:{item.amount}b'
        + (f",tag:{item_tag}" if item_tag else "")
        + "}"
    )
    nbt = (
        f'{{Tags:["{supply_tag}","{record_tag}"],Age:-32768s,'
        f"PickupDelay:0s,Motion:[0.0d,0.0d,0.0d],{item_nbt}}}"
    )
    return f"summon minecraft:item {x} {y} {z} {nbt}"


def _supply_item_tag(item: _SupplyChoice) -> str | None:
    if item.tag:
        return item.tag
    if item.resource == "minecraft:potion":
        potion = (
            "minecraft:strong_healing"
            if item.profile == "strong_healing"
            else "minecraft:healing"
        )
        return f'{{Potion:"{potion}"}}'
    return None


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


def _round_coordinate(value: float) -> float:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return float(int(round(rounded)))
    return rounded


def _format_coordinate(value: float) -> str:
    return f"{value:g}"


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
