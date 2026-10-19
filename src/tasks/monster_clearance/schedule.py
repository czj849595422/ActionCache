"""怪物清剿时间表：解析、校验、seed 生成与 RCON 指令转换。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random
import re
from typing import Any, Sequence

from src.launcher.command_scheduler import BatchPlan, parse_batch_text


class MonsterClearanceScheduleError(ValueError):
    """怪物清剿时间表或生成参数错误。"""


@dataclass(frozen=True)
class EncounterBounds:
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    min_z: int
    max_z: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EncounterBounds":
        if not isinstance(data, dict):
            raise MonsterClearanceScheduleError("bounds 必须是映射")
        keys = ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z")
        if set(data) != set(keys) or any(
            not isinstance(data.get(key), int) for key in keys
        ):
            raise MonsterClearanceScheduleError(
                f"bounds 必须包含整数字段 {list(keys)}"
            )
        bounds = cls(**{key: int(data[key]) for key in keys})
        if (
            bounds.min_x > bounds.max_x
            or bounds.min_y > bounds.max_y
            or bounds.min_z > bounds.max_z
        ):
            raise MonsterClearanceScheduleError("bounds 最小值不能大于最大值")
        return bounds

    def contains(self, x: int, y: int, z: int) -> bool:
        return (
            self.min_x <= x <= self.max_x
            and self.min_y <= y <= self.max_y
            and self.min_z <= z <= self.max_z
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "min_x": self.min_x,
            "max_x": self.max_x,
            "min_y": self.min_y,
            "max_y": self.max_y,
            "min_z": self.min_z,
            "max_z": self.max_z,
        }


@dataclass(frozen=True)
class EncounterSpawnRecord:
    spawn_id: str
    kind: str
    x: int
    y: int
    z: int
    resource: str
    spawn_time_s: float
    amount: int
    profile: str
    line_no: int = 0

    @property
    def position(self) -> tuple[int, int, int]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class MonsterClearanceGenerationSpec:
    bounds: EncounterBounds
    timeout_seconds: int
    monster_count: int
    initial_monster_count: int
    batch_interval_seconds: int
    last_spawn_deadline_seconds: int
    monster_min_distance: float
    agent_positions: tuple[tuple[int, int, int], ...]
    initial_agent_min_distance: float
    supply_drop_min_distance: float
    supply_drop_max_distance: float
    monster_weights: tuple[tuple[str, float], ...]
    zombie_profile_weights: tuple[tuple[str, float], ...]
    min_elite_zombies: int
    max_elite_zombies: int
    supply_weights: tuple[tuple[str, float], ...]
    supply_amounts: tuple[tuple[str, int], ...] = ()
    batch_size: int = 4
    initial_spawn_bounds: EncounterBounds | None = None
    supply_interval_seconds: int = 5
    max_placement_attempts: int = 5000
    max_time_attempts: int = 1000
    agent_spawn_center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    agent_spawn_radius: float = 10.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.monster_count < 1:
            raise MonsterClearanceScheduleError(
                "任务时限必须大于 0，怪物数量至少为 1"
            )
        if not 0 < self.initial_monster_count <= self.monster_count:
            raise MonsterClearanceScheduleError("初始怪物数量非法")
        if self.batch_size < 1:
            raise MonsterClearanceScheduleError("每批刷新数量必须 >= 1")
        if self.batch_interval_seconds <= 0:
            raise MonsterClearanceScheduleError("批量刷新间隔必须大于 0")
        if not (
            0 < self.last_spawn_deadline_seconds < self.timeout_seconds
        ):
            raise MonsterClearanceScheduleError("最后刷新截止时间非法")
        remaining = self.monster_count - self.initial_monster_count
        batches = math.ceil(remaining / self.batch_size) if remaining > 0 else 0
        last_spawn_time = batches * self.batch_interval_seconds
        if last_spawn_time > self.last_spawn_deadline_seconds:
            raise MonsterClearanceScheduleError("批量刷新节奏无法满足最后刷新截止时间")
        if any(
            value <= 0
            for value in (
                self.monster_min_distance,
                self.initial_agent_min_distance,
            )
        ):
            raise MonsterClearanceScheduleError("生成距离必须大于 0")
        if (
            self.supply_drop_min_distance < 0
            or self.supply_drop_max_distance < self.supply_drop_min_distance
        ):
            raise MonsterClearanceScheduleError("补给投放距离范围非法")
        if not self.agent_positions:
            raise MonsterClearanceScheduleError("必须配置 Agent 出生点")
        if any(not self.bounds.contains(*position) for position in self.agent_positions):
            raise MonsterClearanceScheduleError("Agent 出生点必须位于任务范围")
        if any(weight < 0 for _, weight in self.monster_weights):
            raise MonsterClearanceScheduleError("怪物权重不能为负")
        resources = {
            resource
            for resource, weight in self.monster_weights
            if weight > 0
        }
        if (
            not resources
            or not resources <= {"minecraft:zombie", "minecraft:skeleton"}
        ):
            raise MonsterClearanceScheduleError(
                "怪物权重必须至少包含一种正权重怪物（zombie/skeleton）"
            )
        if any(weight < 0 for _, weight in self.zombie_profile_weights):
            raise MonsterClearanceScheduleError("僵尸档案权重不能为负")
        profiles = {
            profile
            for profile, weight in self.zombie_profile_weights
            if weight > 0
        }
        if (
            not profiles
            or not profiles <= {"normal", "elite_iron"}
        ):
            raise MonsterClearanceScheduleError(
                "僵尸档案权重必须至少包含一种正权重档案（normal/elite_iron）"
            )
        if not 0 <= self.min_elite_zombies <= self.max_elite_zombies:
            raise MonsterClearanceScheduleError("精英僵尸数量范围非法")
        if self.min_elite_zombies > self.monster_count - 1:
            raise MonsterClearanceScheduleError("精英僵尸下限超过可用僵尸数量")
        if not self.supply_weights or any(
            weight <= 0 for _, weight in self.supply_weights
        ):
            raise MonsterClearanceScheduleError("掉落道具权重必须非空且为正")
        supply_resources = {resource for resource, _ in self.supply_weights}
        allowed_supply_resources = {
            "minecraft:iron_sword",
            "minecraft:iron_helmet",
            "minecraft:iron_chestplate",
            "minecraft:iron_leggings",
            "minecraft:iron_boots",
            "minecraft:arrow",
            "minecraft:potion",
            "minecraft:bread",
        }
        if not supply_resources <= allowed_supply_resources:
            raise MonsterClearanceScheduleError("掉落道具资源不在允许池内")
        if self.supply_interval_seconds <= 0:
            raise MonsterClearanceScheduleError("掉落间隔必须大于 0")
        if any(not 1 <= amount <= 127 for _, amount in self.supply_amounts):
            raise MonsterClearanceScheduleError("掉落堆叠数量必须在 1..127")
        if self.initial_spawn_bounds is not None:
            region = self.initial_spawn_bounds
            if not (
                region.min_x >= self.bounds.min_x
                and region.max_x <= self.bounds.max_x
                and region.min_z >= self.bounds.min_z
                and region.max_z <= self.bounds.max_z
            ):
                raise MonsterClearanceScheduleError(
                    "初始怪物生成范围必须位于任务范围内"
                )
        if self.max_placement_attempts < 1 or self.max_time_attempts < 1:
            raise MonsterClearanceScheduleError("最大尝试次数必须大于 0")
        if self.agent_spawn_radius <= 0:
            raise MonsterClearanceScheduleError("Agent 出生范围半径必须大于 0")


_ID_RE = re.compile(r"^[mi]\d{4}$")
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_MONSTER_PROFILES = {
    "minecraft:zombie": {"normal", "elite_iron"},
    "minecraft:skeleton": {"default"},
}
_ITEM_PROFILES = {
    "minecraft:arrow": {"default"},
    "minecraft:potion": {"healing"},
    "minecraft:iron_sword": {"default"},
    "minecraft:iron_axe": {"default"},
    "minecraft:iron_helmet": {"default"},
    "minecraft:iron_chestplate": {"default"},
    "minecraft:iron_leggings": {"default"},
    "minecraft:iron_boots": {"default"},
    "minecraft:bread": {"default"},
}


def load_spawn_schedule(
    path: str | Path,
    **validation: Any,
) -> list[EncounterSpawnRecord]:
    """读取九列空格分隔时间表并执行任务约束校验。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise MonsterClearanceScheduleError(
            f"无法读取怪物清剿时间表 {target}: {exc}"
        ) from exc
    records: list[EncounterSpawnRecord] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 9:
            raise MonsterClearanceScheduleError(
                f"第 {line_no} 行必须包含 9 个字段，当前 {len(fields)} 个"
            )
        spawn_id, kind, x, y, z, resource, spawn_time, amount, profile = fields
        try:
            records.append(
                EncounterSpawnRecord(
                    spawn_id=spawn_id,
                    kind=kind,
                    x=int(x),
                    y=int(y),
                    z=int(z),
                    resource=resource,
                    spawn_time_s=float(spawn_time),
                    amount=int(amount),
                    profile=profile,
                    line_no=line_no,
                )
            )
        except ValueError as exc:
            raise MonsterClearanceScheduleError(
                f"第 {line_no} 行数值字段非法: {exc}"
            ) from exc
    validate_spawn_schedule(records, **validation)
    return records


def validate_spawn_schedule(
    records: Sequence[EncounterSpawnRecord],
    *,
    bounds: EncounterBounds | None = None,
    timeout_seconds: float | None = None,
    expected_monster_count: int | None = None,
    initial_monster_count: int | None = None,
    interval_min_seconds: float | None = None,
    interval_max_seconds: float | None = None,
    last_spawn_deadline_seconds: float | None = None,
    monster_min_distance: float | None = None,
    agent_positions: Sequence[tuple[int, int, int]] = (),
    agent_min_distance: float | None = None,
    supply_min_distance: float | None = None,
    initial_agent_min_distance: float | None = None,
    initial_monster_bounds: EncounterBounds | None = None,
    supply_interval_seconds: float | None = None,
    batch_interval_seconds: float | None = None,
) -> None:
    if not records:
        raise MonsterClearanceScheduleError("怪物清剿时间表不能为空")
    ids: set[str] = set()
    positions: set[tuple[int, int, int]] = set()
    for record in records:
        if not _ID_RE.fullmatch(record.spawn_id):
            raise MonsterClearanceScheduleError(
                f"spawn_id {record.spawn_id!r} 必须匹配 m0001/i0001 格式"
            )
        expected_prefix = "m" if record.kind == "monster" else "i"
        if record.kind not in {"monster", "item"} or not record.spawn_id.startswith(
            expected_prefix
        ):
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 的 kind 或 ID 前缀非法"
            )
        if record.spawn_id in ids:
            raise MonsterClearanceScheduleError(f"重复 spawn_id: {record.spawn_id}")
        ids.add(record.spawn_id)
        if record.position in positions:
            raise MonsterClearanceScheduleError(f"重复生成坐标: {record.position}")
        positions.add(record.position)
        profiles = (
            _MONSTER_PROFILES.get(record.resource)
            if record.kind == "monster"
            else _ITEM_PROFILES.get(record.resource)
        )
        if profiles is None or record.profile not in profiles:
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 的资源或档案非法: "
                f"{record.resource}/{record.profile}"
            )
        if record.kind == "monster" and record.amount != 1:
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 怪物 amount 必须为 1"
            )
        if record.kind == "item" and not 1 <= record.amount <= 127:
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 物品 amount 必须在 1..127"
            )
        if record.spawn_time_s < 0:
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 刷新时间不能为负"
            )
        if timeout_seconds is not None and record.spawn_time_s > timeout_seconds:
            raise MonsterClearanceScheduleError(
                f"{record.spawn_id} 刷新时间超过任务时限"
            )
        if bounds is not None and not bounds.contains(*record.position):
            raise MonsterClearanceScheduleError(f"{record.spawn_id} 坐标越界")

    monsters = [record for record in records if record.kind == "monster"]
    supplies = [record for record in records if record.kind == "item"]
    if expected_monster_count is not None and len(monsters) != expected_monster_count:
        raise MonsterClearanceScheduleError(
            f"怪物数量必须为 {expected_monster_count}，当前 {len(monsters)}"
        )
    resources = {record.resource for record in monsters}
    if not resources or not resources <= {"minecraft:zombie", "minecraft:skeleton"}:
        raise MonsterClearanceScheduleError("时间表必须包含僵尸或骷髅")
    elite_count = sum(record.profile == "elite_iron" for record in monsters)
    if not 0 <= elite_count <= 2:
        raise MonsterClearanceScheduleError("精英僵尸数量必须为 0..2")

    ordered_monsters = sorted(monsters, key=lambda item: (item.spawn_time_s, item.line_no))
    if initial_monster_count is not None:
        initial = sum(record.spawn_time_s == 0 for record in monsters)
        if initial != initial_monster_count:
            raise MonsterClearanceScheduleError(
                f"0 秒怪物数量必须为 {initial_monster_count}，当前 {initial}"
            )
    if batch_interval_seconds is not None:
        times = sorted({record.spawn_time_s for record in monsters})
        if not times or times[0] != 0:
            raise MonsterClearanceScheduleError("批量刷新时间表必须从 0 秒开始")
        for left, right in zip(times, times[1:]):
            if abs((right - left) - batch_interval_seconds) > 1e-9:
                raise MonsterClearanceScheduleError(
                    f"批量刷新间隔必须为 {batch_interval_seconds:g} 秒，"
                    f"当前 {right - left:g} 秒"
                )
    elif interval_min_seconds is not None or interval_max_seconds is not None:
        later = ordered_monsters[initial_monster_count:]
        previous = 0.0
        for record in later:
            interval = record.spawn_time_s - previous
            if interval_min_seconds is not None and interval < interval_min_seconds:
                raise MonsterClearanceScheduleError(
                    f"{record.spawn_id} 与上一只怪物间隔过短"
                )
            if interval_max_seconds is not None and interval > interval_max_seconds:
                raise MonsterClearanceScheduleError(
                    f"{record.spawn_id} 与上一只怪物间隔过长"
                )
            previous = record.spawn_time_s
    last_monster_time = max(record.spawn_time_s for record in monsters)
    if (
        last_spawn_deadline_seconds is not None
        and last_monster_time > last_spawn_deadline_seconds
    ):
        raise MonsterClearanceScheduleError("最后一只怪物刷新时间超过截止时间")
    if supply_interval_seconds is not None:
        supply_times = [record.spawn_time_s for record in supplies]
        if supply_times and supply_times[0] != 0:
            raise MonsterClearanceScheduleError("掉落时间表必须从 0 秒开始")
        for left, right in zip(supply_times, supply_times[1:]):
            if abs((right - left) - supply_interval_seconds) > 1e-9:
                raise MonsterClearanceScheduleError(
                    f"掉落间隔必须为 {supply_interval_seconds:g} 秒"
                )
    else:
        if any(
            record.spawn_time_s < 10 or record.spawn_time_s > last_monster_time
            for record in supplies
        ):
            raise MonsterClearanceScheduleError(
                "补给刷新时间必须位于 10 秒至最后怪物之间"
            )

    initial_monsters = [record for record in monsters if record.spawn_time_s == 0]
    if initial_monster_bounds is not None and any(
        not initial_monster_bounds.contains(*record.position)
        for record in initial_monsters
    ):
        raise MonsterClearanceScheduleError("初始怪物坐标超出初始生成范围")
    for index, left in enumerate(initial_monsters):
        if monster_min_distance is not None:
            for right in initial_monsters[index + 1 :]:
                if _horizontal_distance(
                    left.position, right.position
                ) < monster_min_distance:
                    raise MonsterClearanceScheduleError(
                        f"{left.spawn_id} 与初始怪物距离不足 "
                        f"{monster_min_distance:g} 格"
                    )
        if initial_agent_min_distance is not None:
            for agent_position in agent_positions:
                if _horizontal_distance(
                    left.position, agent_position
                ) < initial_agent_min_distance:
                    raise MonsterClearanceScheduleError(
                        f"{left.spawn_id} 距 Agent 出生点不足 "
                        f"{initial_agent_min_distance:g} 格"
                    )
    if agent_min_distance is not None:
        for left in monsters:
            for agent_position in agent_positions:
                if _horizontal_distance(
                    left.position, agent_position
                ) < agent_min_distance:
                    raise MonsterClearanceScheduleError(
                        f"{left.spawn_id} 距 Agent 出生点不足 "
                        f"{agent_min_distance:g} 格"
                    )
    for index, left in enumerate(supplies):
        for right in supplies[index + 1 :]:
            if supply_min_distance is not None and _horizontal_distance(
                left.position, right.position
            ) < supply_min_distance:
                raise MonsterClearanceScheduleError(
                    f"{left.spawn_id} 与 {right.spawn_id} 距离不足 "
                    f"{supply_min_distance:g} 格"
                )
        for right in initial_monsters:
            if supply_min_distance is not None and _horizontal_distance(
                left.position, right.position
            ) < supply_min_distance:
                raise MonsterClearanceScheduleError(
                    f"{left.spawn_id} 与 {right.spawn_id} 距离不足 "
                    f"{supply_min_distance:g} 格"
                )


def generate_spawn_schedule(
    spec: MonsterClearanceGenerationSpec,
    seed: int,
) -> list[EncounterSpawnRecord]:
    """按 seed 生成确定性的怪物时间表。"""
    rng = random.Random(seed)
    monster_times = _generate_batch_monster_times(spec)
    resources = _generate_monster_resources(spec, rng)
    if len(resources) >= 2:
        for required in ("minecraft:zombie", "minecraft:skeleton"):
            if required not in resources:
                replace = next(
                    index
                    for index, resource in enumerate(resources)
                    if resources.count(resource) > 1
                )
                resources[replace] = required
    profiles = _generate_monster_profiles(spec, rng, resources)

    occupied: list[tuple[int, int, int]] = []
    records: list[EncounterSpawnRecord] = []
    for index, (spawn_time, resource, profile) in enumerate(
        zip(monster_times, resources, profiles), start=1
    ):
        if index <= spec.initial_monster_count:
            position = _place_initial_monster_position(spec, rng, occupied)
        else:
            position = _place_unconstrained_position(spec, rng)
        occupied.append(position)
        records.append(
            EncounterSpawnRecord(
                spawn_id=f"m{index:04d}",
                kind="monster",
                x=position[0],
                y=position[1],
                z=position[2],
                resource=resource,
                spawn_time_s=float(spawn_time),
                amount=1,
                profile=profile,
            )
        )

    records.sort(
        key=lambda record: (
            record.spawn_time_s,
            0 if record.kind == "monster" else 1,
            record.spawn_id,
        )
    )
    validate_spawn_schedule(
        records,
        bounds=spec.bounds,
        timeout_seconds=spec.timeout_seconds,
        expected_monster_count=spec.monster_count,
        initial_monster_count=spec.initial_monster_count,
        last_spawn_deadline_seconds=spec.last_spawn_deadline_seconds,
        monster_min_distance=spec.monster_min_distance,
        agent_positions=spec.agent_positions,
        initial_agent_min_distance=spec.initial_agent_min_distance,
        initial_monster_bounds=spec.initial_spawn_bounds,
        batch_interval_seconds=spec.batch_interval_seconds,
    )
    return records


def _generate_batch_monster_times(
    spec: MonsterClearanceGenerationSpec,
) -> list[int]:
    """首批 initial_monster_count 只在 0 秒；其后每批 batch_size 只、间隔固定。"""
    times = [0] * spec.initial_monster_count
    remaining = spec.monster_count - spec.initial_monster_count
    batch_no = 1
    while remaining > 0:
        count = min(spec.batch_size, remaining)
        times.extend([batch_no * spec.batch_interval_seconds] * count)
        remaining -= count
        batch_no += 1
    return times


def _generate_monster_resources(
    spec: MonsterClearanceGenerationSpec,
    rng: random.Random,
) -> list[str]:
    """按权重生成固定配比的怪物列表，再随机打散投放顺序。"""
    total_weight = sum(weight for _, weight in spec.monster_weights)
    if total_weight <= 0:
        raise MonsterClearanceScheduleError("怪物权重总和必须大于 0")
    counts: dict[str, int] = {}
    fractions: list[tuple[float, str]] = []
    assigned = 0
    for resource, weight in spec.monster_weights:
        exact = spec.monster_count * weight / total_weight
        count = math.floor(exact)
        counts[resource] = count
        assigned += count
        fractions.append((exact - count, resource))
    for _, resource in sorted(fractions, key=lambda item: (-item[0], item[1]))[: spec.monster_count - assigned]:
        counts[resource] += 1
    resources = [
        resource
        for resource, count in counts.items()
        for _ in range(count)
    ]
    rng.shuffle(resources)
    return resources


def _generate_monster_profiles(
    spec: MonsterClearanceGenerationSpec,
    rng: random.Random,
    resources: Sequence[str],
) -> list[str]:
    profiles = ["default"] * len(resources)
    zombie_indexes = [
        index for index, resource in enumerate(resources) if resource == "minecraft:zombie"
    ]
    for index in zombie_indexes:
        profiles[index] = rng.choices(
            [profile for profile, _ in spec.zombie_profile_weights],
            weights=[weight for _, weight in spec.zombie_profile_weights],
            k=1,
        )[0]
    elite_indexes = [index for index in zombie_indexes if profiles[index] == "elite_iron"]
    normal_indexes = [index for index in zombie_indexes if profiles[index] == "normal"]
    rng.shuffle(elite_indexes)
    rng.shuffle(normal_indexes)
    while len(elite_indexes) < spec.min_elite_zombies and normal_indexes:
        index = normal_indexes.pop()
        profiles[index] = "elite_iron"
        elite_indexes.append(index)
    while len(elite_indexes) > spec.max_elite_zombies:
        index = elite_indexes.pop()
        profiles[index] = "normal"
    return profiles


def _place_position(
    spec: MonsterClearanceGenerationSpec,
    rng: random.Random,
    *,
    occupied: Sequence[tuple[int, int, int]],
    minimum_distance: float,
) -> tuple[int, int, int]:
    for _ in range(spec.max_placement_attempts):
        candidate = (
            rng.randint(spec.bounds.min_x, spec.bounds.max_x),
            spec.bounds.min_y,
            rng.randint(spec.bounds.min_z, spec.bounds.max_z),
        )
        if any(_horizontal_distance(candidate, other) < minimum_distance for other in occupied):
            continue
        return candidate
    raise MonsterClearanceScheduleError(
        f"{spec.max_placement_attempts} 次尝试后仍无法找到合法生成位置"
    )


def _place_initial_monster_position(
    spec: MonsterClearanceGenerationSpec,
    rng: random.Random,
    occupied: Sequence[tuple[int, int, int]],
) -> tuple[int, int, int]:
    """首批怪物：位于初始生成范围，距 Agent 出生点超过初始最小距离。"""
    region = spec.initial_spawn_bounds or spec.bounds
    for _ in range(spec.max_placement_attempts):
        candidate = (
            rng.randint(region.min_x, region.max_x),
            region.min_y,
            rng.randint(region.min_z, region.max_z),
        )
        if any(
            _horizontal_distance(candidate, other) < spec.monster_min_distance
            for other in occupied
        ):
            continue
        if any(
            _horizontal_distance(candidate, agent)
            < spec.initial_agent_min_distance
            for agent in spec.agent_positions
        ):
            continue
        return candidate
    raise MonsterClearanceScheduleError(
        f"{spec.max_placement_attempts} 次尝试后仍无法找到合法初始怪物位置"
    )


def _place_unconstrained_position(
    spec: MonsterClearanceGenerationSpec,
    rng: random.Random,
) -> tuple[int, int, int]:
    """后续怪物：任务范围内随机位置，无距离限制。"""
    return (
        rng.randint(spec.bounds.min_x, spec.bounds.max_x),
        spec.bounds.min_y,
        rng.randint(spec.bounds.min_z, spec.bounds.max_z),
    )


def spawn_schedule_text(records: Sequence[EncounterSpawnRecord]) -> str:
    lines = ["# spawn_id kind x y z resource spawn_time_s amount profile"]
    lines.extend(
        " ".join(
            (
                record.spawn_id,
                record.kind,
                str(record.x),
                str(record.y),
                str(record.z),
                record.resource,
                _format_number(record.spawn_time_s),
                str(record.amount),
                record.profile,
            )
        )
        for record in records
    )
    return "\n".join(lines) + "\n"


def write_spawn_schedule(
    records: Sequence[EncounterSpawnRecord],
    path: str | Path,
) -> Path:
    target = Path(path)
    target.write_text(spawn_schedule_text(records), encoding="utf-8")
    return target


def command_file_text(
    records: Sequence[EncounterSpawnRecord],
    *,
    monster_tag: str = "ac_monster_quick",
    supply_tag: str = "ac_supply_quick",
    spawn_objective: str = "acTaskSpawn",
) -> str:
    """转换为 command_scheduler 接受的绝对时间召唤指令。"""
    for label, value in (
        ("monster_tag", monster_tag),
        ("supply_tag", supply_tag),
        ("spawn_objective", spawn_objective),
    ):
        if not value or not _COMMAND_NAME_RE.fullmatch(value):
            raise MonsterClearanceScheduleError(f"{label} 只能包含命令安全字符")
    lines = ["# Generated monster clearance schedule: spawn-only events", "[task]"]
    for record in records:
        command = _spawn_command(
            record,
            monster_tag=monster_tag,
            supply_tag=supply_tag,
            spawn_objective=spawn_objective,
        )
        lines.append(f"{_format_number(record.spawn_time_s)}s {command}")
    return "\n".join(lines) + "\n"


def _spawn_command(
    record: EncounterSpawnRecord,
    *,
    monster_tag: str,
    supply_tag: str,
    spawn_objective: str,
) -> str:
    record_tag = f"ac_spawn_{record.spawn_id}"
    if record.kind == "monster":
        tags = f'Tags:["{monster_tag}","{record_tag}"]'
        common = f"{tags},PersistenceRequired:1b,CanPickUpLoot:0b"
        if record.resource == "minecraft:zombie" and record.profile == "elite_iron":
            equipment = _elite_equipment(record)
            hand_item = (
                '{id:"minecraft:iron_sword",Count:1b}'
                if "minecraft:iron_sword" in equipment
                else "{}"
            )
            hand = f"HandItems:[{hand_item},{{}}],HandDropChances:[0.085F,0.0F]"
            armor_slots = (
                "minecraft:iron_boots",
                "minecraft:iron_leggings",
                "minecraft:iron_chestplate",
                "minecraft:iron_helmet",
            )
            armor = (
                "ArmorItems:["
                + ",".join(
                    (
                        f'{{id:"{item}",Count:1b}}'
                        if item in equipment
                        else "{}"
                    )
                    for item in armor_slots
                )
                + "],ArmorDropChances:["
                + ",".join("0.085F" if item in equipment else "0.0F" for item in armor_slots)
                + "]"
            )
            nbt = f"{{{common},IsBaby:0b,{hand},{armor}}}"
        elif record.resource == "minecraft:zombie":
            nbt = (
                f"{{{common},IsBaby:0b,HandItems:[{{}},{{}}],"
                "ArmorItems:[{},{},{},{}]}"
            )
        else:
            nbt = (
                f'{{{common},HandItems:[{{id:"minecraft:bow",Count:1b}},{{}}],'
                "HandDropChances:[0.085F,0.085F]}"
            )
        summon = (
            f"summon {record.resource} {record.x} {record.y} {record.z} {nbt}"
        )
    else:
        item_tag = (
            '{Potion:"minecraft:healing"}'
            if record.resource == "minecraft:potion"
            else None
        )
        item = (
            f'Item:{{id:"{record.resource}",Count:{record.amount}b'
            + (f",tag:{item_tag}" if item_tag else "")
            + "}"
        )
        nbt = (
            f'{{Tags:["{supply_tag}","{record_tag}"],Age:-32768s,'
            f"PickupDelay:0s,Motion:[0.0d,0.0d,0.0d],{item}}}"
        )
        summon = f"summon minecraft:item {record.x} {record.y} {record.z} {nbt}"
    return (
        f"execute store success score #{record.spawn_id} {spawn_objective} run "
        f"{summon}"
    )


def _elite_equipment(record: EncounterSpawnRecord) -> tuple[str, ...]:
    """根据生成记录稳定随机选择 1..3 件精英装备。"""
    seed = (
        record.x * 73856093
        ^ record.y * 19349663
        ^ record.z * 83492791
        ^ int(record.spawn_id[1:])
    )
    rng = random.Random(seed)
    pool = (
        "minecraft:iron_sword",
        "minecraft:iron_helmet",
        "minecraft:iron_chestplate",
        "minecraft:iron_leggings",
        "minecraft:iron_boots",
    )
    return tuple(rng.sample(pool, rng.randint(1, 3)))


def schedule_to_batch_plan(
    records: Sequence[EncounterSpawnRecord],
    **command_options: str,
) -> BatchPlan:
    return parse_batch_text(command_file_text(records, **command_options))


def write_command_file(
    records: Sequence[EncounterSpawnRecord],
    path: str | Path,
    **command_options: str,
) -> Path:
    target = Path(path)
    target.write_text(
        command_file_text(records, **command_options),
        encoding="utf-8",
    )
    return target


def batch_plans_equal(left: BatchPlan, right: BatchPlan) -> bool:
    return left.prepare == right.prepare and [
        (entry.time, entry.unit, entry.command) for entry in left.task
    ] == [
        (entry.time, entry.unit, entry.command) for entry in right.task
    ]


def _horizontal_distance(
    left: tuple[int, int, int],
    right: tuple[int, int, int],
) -> float:
    return math.hypot(left[0] - right[0], left[2] - right[2])


def _format_number(value: float) -> str:
    return f"{value:g}"
