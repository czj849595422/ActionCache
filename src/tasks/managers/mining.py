"""动态矿物采集任务管理器。"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
import re
import threading
import time
from typing import Any, Callable, Iterable

from src.tasks.manager import (
    BaseTaskManager,
    EvaluatorContext,
    TaskOutcome,
    TaskStatus,
)
from src.tasks.mining import MiningBounds, MiningScheduleError
from src.tasks.spec import TaskSpec, TaskSpecError


Clock = Callable[[], float]
Sleeper = Callable[[float], None]

_INITIAL_GROUPS = 8
_MAX_ACTIVE_GROUPS = 12
_LOW_ACTIVE_GROUP_THRESHOLD = 6
_LOW_ACTIVE_GROUP_BATCH_SIZE = 2
_SPAWN_INTERVAL_SECONDS = 3.0
_LIFETIME_MIN_SECONDS = 10
_LIFETIME_MAX_SECONDS = 20
_AGENT_EXCLUSION_DISTANCE = 5.0
_MAX_PLACEMENT_ATTEMPTS = 5000
_DEFAULT_POLL_INTERVAL = 0.1
_MIN_REFRESH_INTERVAL = 1.0
_GROUP_SIZES = (3, 4)
_PRESENCE_SCOREBOARD = "mining_probe"
_ORE_WEIGHTS = {
    "minecraft:lapis_block": 30.0,
    "minecraft:iron_block": 25.0,
    "minecraft:gold_block": 18.0,
    "minecraft:emerald_block": 12.0,
    "minecraft:diamond_block": 10.0,
    "minecraft:netherite_block": 5.0,
}
_SHAPES: dict[int, tuple[tuple[tuple[int, int], ...], ...]] = {
    3: (
        ((0, 0), (1, 0), (2, 0)),
        ((0, 0), (1, 0), (0, 1)),
    ),
    4: (
        ((0, 0), (1, 0), (2, 0), (3, 0)),
        ((0, 0), (1, 0), (2, 0), (0, 1)),
        ((0, 0), (1, 0), (2, 0), (1, 1)),
        ((0, 0), (1, 0), (0, 1), (1, 1)),
    ),
}


@dataclass(frozen=True)
class _OreChoice:
    block: str
    item: str
    value: float
    weight: float


@dataclass(frozen=True)
class _ActiveOreGroup:
    group_id: str
    block: str
    positions: tuple[tuple[int, int, int], ...]
    all_positions: tuple[tuple[int, int, int], ...]
    spawned_at: float
    expires_at: float

    @property
    def duration_s(self) -> float:
        return self.expires_at - self.spawned_at


class MiningManager(BaseTaskManager):
    """运行时刷新矿组，并按团队当前背包价值判定完成。"""

    def __init__(
        self,
        *,
        monotonic: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
        start_runtime_thread: bool = True,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        scheduler_factory: Any | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        _ = scheduler_factory
        super().__init__()
        self._monotonic = monotonic
        self._sleep = sleep
        self._start_runtime_thread = start_runtime_thread
        self._poll_interval = poll_interval
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._runtime_cancel = threading.Event()
        self._server: Any = None
        self._agents: list[Any] = []
        self._bounds: MiningBounds | None = None
        self._ore_blocks: dict[str, str] = {}
        self._ore_choices: tuple[_OreChoice, ...] = ()
        self._initial_positions: dict[str, tuple[float, float]] = {}
        self._generation_seed = 0
        self._rng = random.Random()
        self._active_groups: list[_ActiveOreGroup] = []
        self._last_refresh_at: float | None = None
        self._runtime_thread: threading.Thread | None = None
        self._runtime_error: Exception | None = None
        self._command_errors: list[dict[str, str]] = []
        self._terminal_outcome: TaskOutcome | None = None
        self._next_group_number = 1
        self._next_spawn_due: float | None = None
        self._spawn_attempts = 0
        self._groups_spawned = 0
        self._groups_mined = 0
        self._groups_expired = 0
        self._groups_skipped_capacity = 0
        self._groups_skipped_placement = 0
        self._commands_sent = 0

    def configure(self, spec: TaskSpec) -> None:
        if self._runtime_thread is not None and self._runtime_thread.is_alive():
            raise TaskSpecError("任务调度仍在运行，请先 teardown")
        self._reset_runtime_state()
        super().configure(spec)

    def _validate_spec(self, spec: TaskSpec) -> None:
        if spec.category != "mining":
            raise TaskSpecError("MiningManager 仅支持 mining")
        names = list(spec.agents.get("names") or [])
        count = int(spec.agents.get("count", 0))
        if (
            count < 1
            or len(names) != count
            or not names
            or len(set(names)) != len(names)
        ):
            raise TaskSpecError(
                "mining 要求 agents.count >= 1 且 names 唯一并与 count 一致"
            )
        success = spec.completion.get("success") or {}
        if success.get("type") != "item_value":
            raise TaskSpecError("mining 要求 completion.success.type=item_value")
        params = spec.params or {}
        try:
            bounds = MiningBounds.from_dict(params.get("bounds"))
        except MiningScheduleError as exc:
            raise TaskSpecError(f"mining 范围参数非法: {exc}") from exc
        seed_value = params.get(
            "generation_seed",
            spec.metadata.get("generation_seed", spec.environment.get("seed", 0)),
        )
        if not isinstance(seed_value, int):
            raise TaskSpecError("params.generation_seed 必须是整数")
        ore_blocks = params.get("ore_blocks")
        if not isinstance(ore_blocks, dict) or not ore_blocks or any(
            not isinstance(block, str)
            or not block.startswith("minecraft:")
            or not isinstance(item, str)
            or not item
            for block, item in ore_blocks.items()
        ):
            raise TaskSpecError("params.ore_blocks 必须是 {minecraft:block: item} 映射")
        unknown = [block for block in ore_blocks if block not in _ORE_WEIGHTS]
        if unknown:
            raise TaskSpecError(f"params.ore_blocks 包含未配置权重的矿物: {unknown}")
        item_values = success.get("item_values") or {}
        if set(ore_blocks.values()) != set(item_values):
            raise TaskSpecError("ore_blocks 的掉落物必须与 item_values 完全一致")
        ore_choices = tuple(
            _OreChoice(
                block=block,
                item=str(ore_blocks[block]),
                value=float(item_values[ore_blocks[block]]),
                weight=_ORE_WEIGHTS[block],
            )
            for block in _ORE_WEIGHTS
            if block in ore_blocks
        )
        self._bounds = bounds
        self._generation_seed = seed_value
        self._ore_blocks = dict(ore_blocks)
        self._ore_choices = ore_choices
        self._initial_positions = _initial_agent_positions(spec)

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        actual_names = self._agent_names(agents)
        expected_names = self._agent_names([])
        if len(actual_names) != len(expected_names) or set(actual_names) != set(
            expected_names
        ):
            raise TaskSpecError(
                f"实际 Agent 必须与任务配置一致: expected={expected_names}, actual={actual_names}"
            )
        self._server = server
        self._agents = list(agents)
        super().prepare(server, agents, memory)
        self._send_command(
            f"scoreboard objectives add {_PRESENCE_SCOREBOARD} dummy"
        )

    def start(self) -> None:
        super().start()
        if self._server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        with self._state_lock:
            self._runtime_cancel.clear()
            self._rng = random.Random(self._generation_seed)
            self._active_groups = []
            self._runtime_error = None
            self._command_errors = []
            self._terminal_outcome = None
            self._next_group_number = 1
            self._spawn_attempts = 0
            self._groups_spawned = 0
            self._groups_mined = 0
            self._groups_expired = 0
            self._groups_skipped_capacity = 0
            self._groups_skipped_placement = 0
            self._commands_sent = 0
            now = self._monotonic()
            self._next_spawn_due = now + _SPAWN_INTERVAL_SECONDS
        for _ in range(_INITIAL_GROUPS):
            self._spawn_next_group(now)
        if self._start_runtime_thread:
            self._runtime_thread = threading.Thread(
                target=self._run_runtime,
                name=f"mining-runtime-{self.spec.task_id if self.spec else 'task'}",
                daemon=True,
            )
            self._runtime_thread.start()

    def _run_runtime(self) -> None:
        try:
            while not self._runtime_cancel.is_set():
                now = self._monotonic()
                self._run_runtime_step(now)
                wait_seconds = self._next_wait_seconds()
                if wait_seconds > 0:
                    self._sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._runtime_error = exc

    def _next_wait_seconds(self) -> float:
        with self._state_lock:
            due = self._next_spawn_due
        if due is None:
            return self._poll_interval
        return max(0.0, min(self._poll_interval, due - self._monotonic()))

    def _run_runtime_step(self, now: float | None = None) -> None:
        now = self._monotonic() if now is None else now
        self._refresh_active_groups(now)
        while not self._runtime_cancel.is_set():
            with self._state_lock:
                due = self._next_spawn_due
            if due is None or now < due:
                return
            self._try_spawn_due(due)
            with self._state_lock:
                self._next_spawn_due = due + _SPAWN_INTERVAL_SECONDS
        self._refresh_active_groups(now)

    def _try_spawn_due(self, now: float) -> bool:
        with self._state_lock:
            self._spawn_attempts += 1
            active_count = len(self._active_groups)
            if active_count >= _MAX_ACTIVE_GROUPS:
                self._groups_skipped_capacity += 1
                return False
            batch_size = (
                _LOW_ACTIVE_GROUP_BATCH_SIZE
                if active_count < _LOW_ACTIVE_GROUP_THRESHOLD
                else 1
            )
            batch_size = min(batch_size, _MAX_ACTIVE_GROUPS - active_count)

        spawned = False
        for _ in range(batch_size):
            spawned = self._spawn_next_group(now) or spawned
        return spawned

    def _spawn_next_group(self, now: float) -> bool:
        choice = self._choose_ore()
        duration = float(
            self._rng.randint(_LIFETIME_MIN_SECONDS, _LIFETIME_MAX_SECONDS)
        )
        size = self._rng.choice(_GROUP_SIZES)
        positions = self._choose_positions(size)
        if positions is None:
            with self._state_lock:
                self._groups_skipped_placement += 1
            return False
        with self._state_lock:
            group_id = f"g{self._next_group_number:04d}"
            self._next_group_number += 1
        group = _ActiveOreGroup(
            group_id=group_id,
            block=choice.block,
            positions=positions,
            all_positions=positions,
            spawned_at=now,
            expires_at=now + duration,
        )
        self._place_group(group)
        with self._state_lock:
            self._active_groups.append(group)
            self._groups_spawned += 1
        return True

    def _choose_ore(self) -> _OreChoice:
        if not self._ore_choices:
            raise TaskSpecError("mining 尚未配置矿物")
        return self._rng.choices(
            self._ore_choices,
            weights=[choice.weight for choice in self._ore_choices],
            k=1,
        )[0]

    def _choose_positions(
        self,
        size: int,
    ) -> tuple[tuple[int, int, int], ...] | None:
        if self._bounds is None:
            raise TaskSpecError("mining 尚未配置 bounds")
        agent_positions = self._agent_positions()
        for _ in range(_MAX_PLACEMENT_ATTEMPTS):
            base_shape = self._rng.choice(_SHAPES[size])
            shape = _rotate_shape(base_shape, self._rng.randrange(4))
            anchor = (
                self._rng.randint(self._bounds.min_x, self._bounds.max_x),
                self._bounds.min_y,
                self._rng.randint(self._bounds.min_z, self._bounds.max_z),
            )
            positions = tuple(
                (anchor[0] + dx, anchor[1], anchor[2] + dz) for dx, dz in shape
            )
            if any(not self._bounds.contains(*position) for position in positions):
                continue
            if self._positions_are_occupied(positions):
                continue
            if _too_close_to_agents(positions, agent_positions):
                continue
            return (anchor,) + tuple(sorted(set(positions) - {anchor}))
        return None

    def _positions_are_occupied(
        self,
        positions: Iterable[tuple[int, int, int]],
    ) -> bool:
        requested = set(positions)
        with self._state_lock:
            occupied = {
                position
                for group in self._active_groups
                for position in group.positions
            }
        return bool(requested & occupied)

    def _agent_positions(self) -> list[tuple[float, float]]:
        positions: list[tuple[float, float]] = []
        seen: set[str] = set()
        for agent in self._agents:
            name = str(getattr(agent, "username", agent))
            position = None
            raw = getattr(agent, "get_raw_perception", None)
            if callable(raw):
                try:
                    position = _extract_position(raw())
                except Exception:  # noqa: BLE001
                    position = None
            if position is not None:
                positions.append(position)
                seen.add(name)
                continue
            fallback = self._initial_positions.get(name)
            if fallback is not None:
                positions.append(fallback)
                seen.add(name)
        for name, fallback in self._initial_positions.items():
            if name not in seen:
                positions.append(fallback)
        return positions

    def _place_group(self, group: _ActiveOreGroup) -> None:
        for x, y, z in group.positions:
            self._send_command(
                f"execute if block {x} {y} {z} air run "
                f"setblock {x} {y} {z} {group.block}"
            )

    def _refresh_active_groups(self, now: float | None = None) -> None:
        """Refresh group membership, removing mined and expired groups."""
        with self._refresh_lock:
            now = self._monotonic() if now is None else now
            with self._state_lock:
                if (
                    self._last_refresh_at is not None
                    and now - self._last_refresh_at < _MIN_REFRESH_INTERVAL
                ):
                    return
                self._last_refresh_at = now
            with self._state_lock:
                groups = list(self._active_groups)

            checks = [
                (group.block, position)
                for group in groups
                for position in group.positions
            ]
            present = self._blocks_are_present(checks)
            present_by_group: dict[str, set[tuple[int, int, int]]] = {}
            offset = 0
            for group in groups:
                present_by_group[group.group_id] = {
                    position
                    for position, is_present in zip(
                        group.positions,
                        present[offset : offset + len(group.positions)],
                    )
                    if is_present
                }
                offset += len(group.positions)

            refreshed: list[_ActiveOreGroup] = []
            mined_count = 0
            expired_groups: list[_ActiveOreGroup] = []
            for group in groups:
                remaining = tuple(
                    position
                    for position in group.positions
                    if position in present_by_group[group.group_id]
                )
                if not remaining:
                    mined_count += 1
                    continue
                if now >= group.expires_at:
                    expired_groups.append(
                        _ActiveOreGroup(
                            group_id=group.group_id,
                            block=group.block,
                            positions=remaining,
                            all_positions=group.all_positions,
                            spawned_at=group.spawned_at,
                            expires_at=group.expires_at,
                        )
                    )
                    continue
                if remaining != group.positions:
                    group = _ActiveOreGroup(
                        group_id=group.group_id,
                        block=group.block,
                        positions=remaining,
                        all_positions=group.all_positions,
                        spawned_at=group.spawned_at,
                        expires_at=group.expires_at,
                    )
                refreshed.append(group)

            for group in expired_groups:
                self._clear_group(group)

            with self._state_lock:
                refreshed_ids = {group.group_id for group in groups}
                concurrent_groups = [
                    group
                    for group in self._active_groups
                    if group.group_id not in refreshed_ids
                ]
                self._active_groups = refreshed + concurrent_groups
                self._groups_mined += mined_count
                self._groups_expired += len(expired_groups)

    def _block_is_present(
        self,
        position: tuple[int, int, int],
        block: str,
    ) -> bool:
        return self._blocks_are_present([(block, position)])[0]

    def _blocks_are_present(
        self,
        checks: list[tuple[str, tuple[int, int, int]]],
    ) -> list[bool]:
        if not checks:
            return []
        commands = [
            f"scoreboard players reset * {_PRESENCE_SCOREBOARD}"
        ]
        probe_names: list[str] = []
        for index, (block, (x, y, z)) in enumerate(checks):
            probe_name = f"#mining_probe_{index}"
            probe_names.append(probe_name)
            commands.append(
                f"execute store success score {probe_name} "
                f"{_PRESENCE_SCOREBOARD} if block {x} {y} {z} {block} run "
                f"scoreboard players set {probe_name} {_PRESENCE_SCOREBOARD} 1"
            )
        commands.append("scoreboard players list")
        server = self._server
        if server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        try:
            send_commands = getattr(server, "send_commands", None)
            if callable(send_commands):
                responses = list(send_commands(commands))
            else:
                responses = [server.send_command(command) for command in commands]
            if len(responses) != len(commands):
                raise RuntimeError(
                    f"批量 RCON 响应数量不一致: expected={len(commands)}, actual={len(responses)}"
                )
            with self._state_lock:
                self._commands_sent += len(commands)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._command_errors.append(
                    {"command": "<batch block presence check>", "error": str(exc)}
                )
            # 查询失败不能证明方块已被挖走，保留全部坐标避免误删。
            return [True] * len(commands)

        tracked = str(responses[-1] or "")
        return [
            bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", tracked))
            for name in probe_names
        ]

    def _clear_group(self, group: _ActiveOreGroup) -> None:
        for x, y, z in group.positions:
            self._send_command(
                f"execute if block {x} {y} {z} {group.block} run "
                f"setblock {x} {y} {z} air"
            )

    def _send_command(self, command: str) -> None:
        server = self._server
        if server is None:
            raise TaskSpecError("任务服务器尚未 prepare")
        try:
            server.send_command(command)
            with self._state_lock:
                self._commands_sent += 1
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._command_errors.append(
                    {"command": command, "error": str(exc)}
                )

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if not self._started:
            raise TaskSpecError("任务管理器尚未 start")
        with self._state_lock:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
            runtime_error = self._runtime_error
            command_errors = list(self._command_errors)
        if runtime_error is not None or command_errors:
            detail: dict[str, Any] = {}
            if runtime_error is not None:
                detail["error"] = str(runtime_error)
            if command_errors:
                detail["command_errors"] = command_errors
            return self._finish(
                TaskOutcome(TaskStatus.FAILURE, reason="schedule_failed", detail=detail)
            )

        value_metrics = self._value_metrics(context)
        target = float(self.spec.completion["success"]["target_value"])
        if value_metrics["current_value"] >= target:
            return self._finish(
                TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="item_value",
                    detail=value_metrics,
                )
            )
        if context.elapsed_seconds() >= float(
            self.spec.completion["timeout_seconds"]
        ):
            return self._finish(
                TaskOutcome(
                    TaskStatus.TIMEOUT,
                    reason="timeout",
                    detail=value_metrics,
                )
            )
        return TaskOutcome(TaskStatus.RUNNING, detail=value_metrics)

    def _finish(self, outcome: TaskOutcome) -> TaskOutcome:
        with self._state_lock:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
            self._terminal_outcome = outcome
        self._stop_runtime(cleanup=True)
        return outcome

    def _value_metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        if self.spec is None:
            return {}
        totals = self._inventory_totals(context)
        values = self.spec.completion["success"]["item_values"]
        item_counts = {item: int(totals.get(item, 0)) for item in values}
        item_value_totals = {
            item: item_counts[item] * float(value) for item, value in values.items()
        }
        current = sum(item_value_totals.values())
        return {
            "current_value": _number(current),
            "target_value": _number(
                float(self.spec.completion["success"]["target_value"])
            ),
            "item_counts": item_counts,
            "item_values": {item: _number(float(value)) for item, value in values.items()},
            "item_value_totals": {
                item: _number(value) for item, value in item_value_totals.items()
            },
        }

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        self._refresh_active_groups()
        metrics = self._value_metrics(context)
        elapsed = max(0.0, float(context.elapsed_seconds()))
        with self._state_lock:
            active = list(self._active_groups)
            runtime_error = self._runtime_error
            command_errors = list(self._command_errors)
            thread = self._runtime_thread
            next_due = self._next_spawn_due
            counters = {
                "groups_spawned": self._groups_spawned,
                "groups_expired": self._groups_expired,
                "spawn_attempts": self._spawn_attempts,
                "groups_skipped_capacity": self._groups_skipped_capacity,
                "groups_skipped_placement": self._groups_skipped_placement,
                "commands_sent": self._commands_sent,
                "groups_mined": self._groups_mined,
            }
        metrics.update(
            {
                "elapsed_seconds": elapsed,
                "active_groups": len(active),
                "active_blocks": sum(len(group.positions) for group in active),
                "active_ore_groups": [
                    {
                        "group_id": group.group_id,
                        "block": group.block,
                        "positions": [
                            {"x": x, "y": y, "z": z}
                            for x, y, z in group.positions
                        ],
                        "all_positions": [
                            {"x": x, "y": y, "z": z}
                            for x, y, z in group.all_positions
                        ],
                    }
                    for group in active
                ],
                "command_errors": len(command_errors),
                "runtime_error": str(runtime_error) if runtime_error else None,
                "runtime_thread_alive": bool(thread and thread.is_alive()),
                "next_spawn_in_seconds": (
                    _number(max(0.0, next_due - self._monotonic()))
                    if next_due is not None
                    else None
                ),
                **counters,
            }
        )
        return metrics

    def teardown(self) -> None:
        self._stop_runtime(cleanup=True)
        self._reset_runtime_state()
        super().teardown()

    def _stop_runtime(self, *, cleanup: bool) -> None:
        self._runtime_cancel.set()
        thread = self._runtime_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=12.0)
        if cleanup:
            self._cleanup_active_groups()

    def _cleanup_active_groups(self) -> None:
        with self._state_lock:
            groups = list(self._active_groups)
            self._active_groups = []
        for group in groups:
            self._clear_group(group)
        if self._server is not None:
            self._send_command(
                f"scoreboard objectives remove {_PRESENCE_SCOREBOARD}"
            )

    def _reset_runtime_state(self) -> None:
        self._runtime_cancel = threading.Event()
        self._server = None
        self._agents = []
        self._bounds = None
        self._ore_blocks = {}
        self._ore_choices = ()
        self._initial_positions = {}
        self._generation_seed = 0
        self._rng = random.Random()
        self._active_groups = []
        self._last_refresh_at = None
        self._runtime_thread = None
        self._runtime_error = None
        self._command_errors = []
        self._terminal_outcome = None
        self._next_group_number = 1
        self._next_spawn_due = None
        self._spawn_attempts = 0
        self._groups_spawned = 0
        self._groups_mined = 0
        self._groups_expired = 0
        self._groups_skipped_capacity = 0
        self._groups_skipped_placement = 0
        self._commands_sent = 0


def _initial_agent_positions(spec: TaskSpec) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    per_agent = spec.initial_state.get("per_agent") or {}
    if isinstance(per_agent, dict):
        default = _position_to_xz(per_agent.get("position"))
        if default is not None:
            for name in spec.agents.get("names") or []:
                result[str(name)] = default
    for item in spec.initial_state.get("by_agent") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        position = _position_to_xz(item.get("position"))
        if position is not None:
            result[str(item["name"])] = position
    return result


def _extract_position(snapshot: Any) -> tuple[float, float] | None:
    if not isinstance(snapshot, dict):
        return None
    self_state = snapshot.get("self")
    if isinstance(self_state, dict):
        position = _position_to_xz(self_state.get("position"))
        if position is not None:
            return position
    return _position_to_xz(snapshot.get("position"))


def _position_to_xz(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    x = value.get("x")
    z = value.get("z")
    if not isinstance(x, (int, float)) or not isinstance(z, (int, float)):
        return None
    return (float(x), float(z))


def _too_close_to_agents(
    positions: Iterable[tuple[int, int, int]],
    agent_positions: Iterable[tuple[float, float]],
) -> bool:
    agents = list(agent_positions)
    if not agents:
        return False
    return any(
        math.hypot(x - agent_x, z - agent_z) < _AGENT_EXCLUSION_DISTANCE
        for x, _y, z in positions
        for agent_x, agent_z in agents
    )


def _rotate_shape(
    shape: Iterable[tuple[int, int]],
    quarter_turns: int,
) -> tuple[tuple[int, int], ...]:
    rotated = list(shape)
    for _ in range(quarter_turns):
        rotated = [(-z, x) for x, z in rotated]
    return tuple(rotated)


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
