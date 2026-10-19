"""动态矿物时间表：解析、校验、seed 生成与 RCON 指令转换。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random
from typing import Any, Iterable, Sequence

from src.launcher.command_scheduler import BatchPlan, parse_batch_text


class MiningScheduleError(ValueError):
    """矿物时间表或生成参数错误。"""


@dataclass(frozen=True)
class MiningBounds:
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    min_z: int
    max_z: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MiningBounds":
        if not isinstance(data, dict):
            raise MiningScheduleError("bounds 必须是映射")
        keys = ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z")
        if set(data) != set(keys) or any(
            not isinstance(data.get(key), int) for key in keys
        ):
            raise MiningScheduleError(f"bounds 必须包含整数字段 {list(keys)}")
        bounds = cls(**{key: int(data[key]) for key in keys})
        if (
            bounds.min_x > bounds.max_x
            or bounds.min_y > bounds.max_y
            or bounds.min_z > bounds.max_z
        ):
            raise MiningScheduleError("bounds 最小值不能大于最大值")
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
class OreSpawnRecord:
    group_id: str
    x: int
    y: int
    z: int
    block: str
    spawn_time_s: float
    duration_s: float
    line_no: int = 0

    @property
    def despawn_time_s(self) -> float:
        return self.spawn_time_s + self.duration_s

    @property
    def position(self) -> tuple[int, int, int]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class OreChoice:
    block: str
    item: str
    value: float
    weight: float


@dataclass(frozen=True)
class MiningGenerationSpec:
    bounds: MiningBounds
    timeout_seconds: int
    target_value: float
    wave_interval_seconds: int
    groups_per_wave: int
    duration_min_seconds: int
    duration_max_seconds: int
    group_sizes: tuple[int, ...]
    min_anchor_distance: float
    ores: tuple[OreChoice, ...]
    exclusion_centers: tuple[tuple[int, int], ...] = ()
    exclusion_radius: float = 0.0
    max_placement_attempts: int = 5000

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.target_value <= 0:
            raise MiningScheduleError("timeout_seconds 和 target_value 必须大于 0")
        if self.wave_interval_seconds <= 0 or self.groups_per_wave <= 0:
            raise MiningScheduleError("刷新间隔和每波矿组数必须大于 0")
        if not (0 < self.duration_min_seconds <= self.duration_max_seconds):
            raise MiningScheduleError("矿组持续时间范围非法")
        if self.duration_max_seconds > self.timeout_seconds:
            raise MiningScheduleError("最大持续时间不能超过任务时限")
        if not self.group_sizes or any(size not in (3, 4) for size in self.group_sizes):
            raise MiningScheduleError("group_sizes 仅支持 3 和 4")
        if self.min_anchor_distance <= 0:
            raise MiningScheduleError("min_anchor_distance 必须大于 0")
        if not self.ores:
            raise MiningScheduleError("至少配置一种矿物")
        if any(
            not ore.block.startswith("minecraft:")
            or not ore.item
            or ore.value <= 0
            or ore.weight <= 0
            for ore in self.ores
        ):
            raise MiningScheduleError("矿物名称、物品名、价值或权重非法")
        if self.exclusion_radius < 0 or self.max_placement_attempts < 1:
            raise MiningScheduleError("排除半径或最大放置尝试次数非法")


@dataclass(frozen=True)
class _Group:
    group_id: str
    anchor: tuple[int, int, int]
    block: str
    spawn_time_s: float
    duration_s: float
    positions: frozenset[tuple[int, int, int]]

    @property
    def despawn_time_s(self) -> float:
        return self.spawn_time_s + self.duration_s


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


def load_spawn_schedule(
    path: str | Path,
    *,
    bounds: MiningBounds | None = None,
    min_anchor_distance: float | None = None,
    allowed_blocks: set[str] | None = None,
    timeout_seconds: float | None = None,
) -> list[OreSpawnRecord]:
    """读取空格分隔时间表并执行结构及可选任务约束校验。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise MiningScheduleError(f"无法读取矿物时间表 {target}: {exc}") from exc
    records: list[OreSpawnRecord] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 7:
            raise MiningScheduleError(
                f"第 {line_no} 行必须包含 7 个字段，当前 {len(fields)} 个"
            )
        group_id, x, y, z, block, spawn_time, duration = fields
        try:
            record = OreSpawnRecord(
                group_id=group_id,
                x=int(x),
                y=int(y),
                z=int(z),
                block=block,
                spawn_time_s=float(spawn_time),
                duration_s=float(duration),
                line_no=line_no,
            )
        except ValueError as exc:
            raise MiningScheduleError(f"第 {line_no} 行数值字段非法: {exc}") from exc
        records.append(record)
    validate_spawn_schedule(
        records,
        bounds=bounds,
        min_anchor_distance=min_anchor_distance,
        allowed_blocks=allowed_blocks,
        timeout_seconds=timeout_seconds,
    )
    return records


def validate_spawn_schedule(
    records: Sequence[OreSpawnRecord],
    *,
    bounds: MiningBounds | None = None,
    min_anchor_distance: float | None = None,
    allowed_blocks: set[str] | None = None,
    timeout_seconds: float | None = None,
) -> list[_Group]:
    if not records:
        raise MiningScheduleError("矿物时间表不能为空")
    groups = _group_records(records)
    for group in groups:
        if not group.group_id or any(char.isspace() for char in group.group_id):
            raise MiningScheduleError("group_id 不能为空或包含空白")
        if not group.block.startswith("minecraft:"):
            raise MiningScheduleError(f"{group.group_id} 方块必须使用 minecraft: 命名空间")
        if allowed_blocks is not None and group.block not in allowed_blocks:
            raise MiningScheduleError(f"{group.group_id} 使用未配置矿物 {group.block}")
        if group.spawn_time_s < 0 or group.duration_s <= 0:
            raise MiningScheduleError(f"{group.group_id} 刷新时间或持续时间非法")
        if timeout_seconds is not None and group.despawn_time_s > timeout_seconds:
            raise MiningScheduleError(f"{group.group_id} 消失时间超过任务时限")
        if bounds is not None and any(
            not bounds.contains(*position) for position in group.positions
        ):
            raise MiningScheduleError(f"{group.group_id} 存在越界方块")
        if not _is_face_connected(group.positions):
            raise MiningScheduleError(f"{group.group_id} 的方块不是面连通结构")

    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            if not _lifetimes_overlap(left, right):
                continue
            if left.positions & right.positions:
                raise MiningScheduleError(
                    f"{left.group_id} 与 {right.group_id} 在同时存活期间坐标重叠"
                )
            if min_anchor_distance is not None:
                distance = math.hypot(
                    left.anchor[0] - right.anchor[0],
                    left.anchor[2] - right.anchor[2],
                )
                if distance < min_anchor_distance:
                    raise MiningScheduleError(
                        f"{left.group_id} 与 {right.group_id} 锚点距离 {distance:.3f} "
                        f"小于 {min_anchor_distance:g}"
                    )
    return groups


def _group_records(records: Sequence[OreSpawnRecord]) -> list[_Group]:
    chunks: list[list[OreSpawnRecord]] = []
    closed: set[str] = set()
    current: list[OreSpawnRecord] = []
    for record in records:
        if current and record.group_id != current[0].group_id:
            closed.add(current[0].group_id)
            chunks.append(current)
            current = []
        if record.group_id in closed:
            raise MiningScheduleError(f"group_id {record.group_id} 必须连续出现")
        current.append(record)
    if current:
        chunks.append(current)

    groups: list[_Group] = []
    for chunk in chunks:
        first = chunk[0]
        if len(chunk) not in (3, 4):
            raise MiningScheduleError(f"{first.group_id} 必须包含 3 或 4 个方块")
        if any(
            item.block != first.block
            or item.spawn_time_s != first.spawn_time_s
            or item.duration_s != first.duration_s
            for item in chunk[1:]
        ):
            raise MiningScheduleError(f"{first.group_id} 的矿物或时间字段不一致")
        positions = frozenset(item.position for item in chunk)
        if len(positions) != len(chunk):
            raise MiningScheduleError(f"{first.group_id} 包含重复坐标")
        groups.append(
            _Group(
                group_id=first.group_id,
                anchor=first.position,
                block=first.block,
                spawn_time_s=first.spawn_time_s,
                duration_s=first.duration_s,
                positions=positions,
            )
        )
    return groups


def _lifetimes_overlap(left: _Group, right: _Group) -> bool:
    return max(left.spawn_time_s, right.spawn_time_s) < min(
        left.despawn_time_s,
        right.despawn_time_s,
    )


def _is_face_connected(positions: Iterable[tuple[int, int, int]]) -> bool:
    remaining = set(positions)
    if not remaining:
        return False
    pending = [remaining.pop()]
    while pending:
        x, y, z = pending.pop()
        neighbors = {
            (x + 1, y, z),
            (x - 1, y, z),
            (x, y + 1, z),
            (x, y - 1, z),
            (x, y, z + 1),
            (x, y, z - 1),
        }
        found = remaining & neighbors
        remaining -= found
        pending.extend(found)
    return not remaining


def generate_spawn_schedule(
    spec: MiningGenerationSpec,
    seed: int,
) -> list[OreSpawnRecord]:
    """按 seed 生成确定性的矿组时间表。"""
    rng = random.Random(seed)
    records: list[OreSpawnRecord] = []
    groups: list[_Group] = []
    group_number = 1
    last_spawn = spec.timeout_seconds - spec.duration_max_seconds
    for spawn_time in range(0, last_spawn + 1, spec.wave_interval_seconds):
        for _ in range(spec.groups_per_wave):
            size = rng.choice(spec.group_sizes)
            duration = rng.randint(
                spec.duration_min_seconds,
                spec.duration_max_seconds,
            )
            ore = rng.choices(
                spec.ores,
                weights=[choice.weight for choice in spec.ores],
                k=1,
            )[0]
            group_id = f"g{group_number:04d}"
            group_number += 1
            group = _place_group(
                spec,
                rng,
                group_id=group_id,
                block=ore.block,
                size=size,
                spawn_time=float(spawn_time),
                duration=float(duration),
                existing=groups,
            )
            groups.append(group)
            ordered_positions = [group.anchor] + sorted(
                group.positions - {group.anchor}
            )
            for x, y, z in ordered_positions:
                records.append(
                    OreSpawnRecord(
                        group_id=group.group_id,
                        x=x,
                        y=y,
                        z=z,
                        block=group.block,
                        spawn_time_s=group.spawn_time_s,
                        duration_s=group.duration_s,
                    )
                )

    validate_spawn_schedule(
        records,
        bounds=spec.bounds,
        min_anchor_distance=spec.min_anchor_distance,
        allowed_blocks={ore.block for ore in spec.ores},
        timeout_seconds=spec.timeout_seconds,
    )
    total_value = sum(
        next(ore.value for ore in spec.ores if ore.block == record.block)
        for record in records
    )
    if total_value < spec.target_value:
        raise MiningScheduleError(
            f"时间表理论总价值 {total_value:g} 低于目标 {spec.target_value:g}"
        )
    return records


def _place_group(
    spec: MiningGenerationSpec,
    rng: random.Random,
    *,
    group_id: str,
    block: str,
    size: int,
    spawn_time: float,
    duration: float,
    existing: Sequence[_Group],
) -> _Group:
    for _ in range(spec.max_placement_attempts):
        base_shape = rng.choice(_SHAPES[size])
        shape = _rotate_shape(base_shape, rng.randrange(4))
        anchor = (
            rng.randint(spec.bounds.min_x, spec.bounds.max_x),
            spec.bounds.min_y,
            rng.randint(spec.bounds.min_z, spec.bounds.max_z),
        )
        positions = frozenset(
            (anchor[0] + dx, anchor[1], anchor[2] + dz) for dx, dz in shape
        )
        if any(not spec.bounds.contains(*position) for position in positions):
            continue
        if any(
            math.hypot(anchor[0] - x, anchor[2] - z) < spec.exclusion_radius
            for x, z in spec.exclusion_centers
        ):
            continue
        candidate = _Group(
            group_id=group_id,
            anchor=anchor,
            block=block,
            spawn_time_s=spawn_time,
            duration_s=duration,
            positions=positions,
        )
        conflict = False
        for other in existing:
            if not _lifetimes_overlap(candidate, other):
                continue
            distance = math.hypot(
                candidate.anchor[0] - other.anchor[0],
                candidate.anchor[2] - other.anchor[2],
            )
            if distance < spec.min_anchor_distance or candidate.positions & other.positions:
                conflict = True
                break
        if not conflict:
            return candidate
    raise MiningScheduleError(
        f"{group_id} 在 {spec.max_placement_attempts} 次尝试后仍无法找到合法位置"
    )


def _rotate_shape(
    shape: Sequence[tuple[int, int]],
    quarter_turns: int,
) -> tuple[tuple[int, int], ...]:
    rotated = list(shape)
    for _ in range(quarter_turns):
        rotated = [(-z, x) for x, z in rotated]
    return tuple(rotated)


def spawn_schedule_text(records: Sequence[OreSpawnRecord]) -> str:
    lines = ["# group_id x y z block spawn_time_s duration_s"]
    lines.extend(
        " ".join(
            (
                record.group_id,
                str(record.x),
                str(record.y),
                str(record.z),
                record.block,
                _format_number(record.spawn_time_s),
                _format_number(record.duration_s),
            )
        )
        for record in records
    )
    return "\n".join(lines) + "\n"


def write_spawn_schedule(
    records: Sequence[OreSpawnRecord],
    path: str | Path,
) -> Path:
    target = Path(path)
    target.write_text(spawn_schedule_text(records), encoding="utf-8")
    return target


def command_file_text(records: Sequence[OreSpawnRecord]) -> str:
    """转换为 command_scheduler 接受的绝对时间任务文件。"""
    events: list[tuple[float, int, int, str]] = []
    for index, record in enumerate(records):
        spawn = (
            f"execute if block {record.x} {record.y} {record.z} air run "
            f"setblock {record.x} {record.y} {record.z} {record.block}"
        )
        despawn = (
            f"execute if block {record.x} {record.y} {record.z} {record.block} run "
            f"setblock {record.x} {record.y} {record.z} air"
        )
        events.append((record.despawn_time_s, 0, index, despawn))
        events.append((record.spawn_time_s, 1, index, spawn))
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    lines = [
        "# Generated mining schedule: despawn events precede spawn events at equal times",
        "[task]",
    ]
    lines.extend(f"{_format_number(event[0])}s {event[3]}" for event in events)
    return "\n".join(lines) + "\n"


def schedule_to_batch_plan(records: Sequence[OreSpawnRecord]) -> BatchPlan:
    return parse_batch_text(command_file_text(records))


def write_command_file(
    records: Sequence[OreSpawnRecord],
    path: str | Path,
) -> Path:
    target = Path(path)
    target.write_text(command_file_text(records), encoding="utf-8")
    return target


def batch_plans_equal(left: BatchPlan, right: BatchPlan) -> bool:
    return left.prepare == right.prepare and [
        (entry.time, entry.unit, entry.command) for entry in left.task
    ] == [
        (entry.time, entry.unit, entry.command) for entry in right.task
    ]


def _format_number(value: float) -> str:
    return f"{value:g}"
