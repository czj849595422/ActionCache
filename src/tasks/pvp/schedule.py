"""PVP supply drop model and summon command conversion."""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any, Mapping, Sequence

from src.tasks.monster_clearance import EncounterBounds


class PVPSupplyError(ValueError):
    """Invalid PVP supply generation configuration."""


@dataclass(frozen=True)
class PVPDropRecord:
    spawn_id: str
    x: int
    y: int
    z: int
    resource: str
    amount: int
    profile: str
    item_class: str
    elapsed_seconds: float

    @property
    def position(self) -> tuple[int, int, int]:
        return (self.x, self.y, self.z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spawn_id": self.spawn_id,
            "position": {"x": self.x, "y": self.y, "z": self.z},
            "resource": self.resource,
            "amount": self.amount,
            "profile": self.profile,
            "item_class": self.item_class,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class PVPDropConfig:
    bounds: EncounterBounds
    supply_tag: str = "ac_pvp_sup"
    drops_per_wave: int = 5
    min_drop_spacing: float = 20.0
    placement_attempts_per_drop: int = 128
    gear_probability_start: float = 0.8
    gear_probability_end: float = 0.2
    probability_decay_seconds: float = 300.0
    food_healing_disabled_after_seconds: float = 300.0
    ammo_share_before_disable: float = 0.6
    food_share_before_disable: float = 0.25
    healing_share_before_disable: float = 0.15

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "PVPDropConfig":
        try:
            bounds = EncounterBounds.from_dict(params.get("bounds"))
            config = cls(
                bounds=bounds,
                supply_tag=str(params.get("supply_tag") or "ac_pvp_sup"),
                drops_per_wave=int(params.get("drops_per_wave", 5)),
                min_drop_spacing=float(params.get("min_drop_spacing", 20.0)),
                placement_attempts_per_drop=int(
                    params.get("placement_attempts_per_drop", 128)
                ),
                gear_probability_start=float(
                    params.get("gear_probability_start", 0.8)
                ),
                gear_probability_end=float(params.get("gear_probability_end", 0.2)),
                probability_decay_seconds=float(
                    params.get("probability_decay_seconds", 300.0)
                ),
                food_healing_disabled_after_seconds=float(
                    params.get("food_healing_disabled_after_seconds", 300.0)
                ),
                ammo_share_before_disable=float(
                    params.get("ammo_share_before_disable", 0.6)
                ),
                food_share_before_disable=float(
                    params.get("food_share_before_disable", 0.25)
                ),
                healing_share_before_disable=float(
                    params.get("healing_share_before_disable", 0.15)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise PVPSupplyError(f"PVP 掉落参数非法: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        for label, value in (
            ("gear_probability_start", self.gear_probability_start),
            ("gear_probability_end", self.gear_probability_end),
        ):
            if not 0 <= value <= 1:
                raise PVPSupplyError(f"{label} 必须在 [0, 1] 内")
        if self.probability_decay_seconds <= 0:
            raise PVPSupplyError("probability_decay_seconds 必须大于 0")
        if self.drops_per_wave < 1:
            raise PVPSupplyError("drops_per_wave 必须 >= 1")
        if self.min_drop_spacing < 0:
            raise PVPSupplyError("min_drop_spacing 不能为负数")
        if self.placement_attempts_per_drop < 1:
            raise PVPSupplyError("placement_attempts_per_drop 必须 >= 1")
        if self.food_healing_disabled_after_seconds < 0:
            raise PVPSupplyError(
                "food_healing_disabled_after_seconds 不能为负数"
            )
        shares = (
            self.ammo_share_before_disable,
            self.food_share_before_disable,
            self.healing_share_before_disable,
        )
        if any(value < 0 for value in shares) or sum(shares) <= 0:
            raise PVPSupplyError("消耗品子类概率必须非负且总和大于 0")


PVP_SUPPLY_ITEM_NAMES = frozenset(
    {
        "wooden_sword",
        "stone_sword",
        "iron_sword",
        "bow",
        "arrow",
        "leather_helmet",
        "leather_chestplate",
        "leather_leggings",
        "leather_boots",
        "potion",
        "bread",
        "cooked_beef",
        "apple",
    }
)

_WEAPON_MATERIALS = (
    ("wooden", 45.0),
    ("stone", 35.0),
    ("iron", 20.0),
)
_ARMOR_MATERIALS = (
    ("leather", 1.0),
)
_ARMOR_PIECES = ("helmet", "chestplate", "leggings", "boots")
_FOOD_ITEMS = (
    ("minecraft:bread", 2, "default", 45.0),
    ("minecraft:cooked_beef", 2, "default", 35.0),
    ("minecraft:apple", 3, "default", 20.0),
)
_HEALING_ITEMS = (
    ("minecraft:potion", 1, "healing", 80.0),
    ("minecraft:potion", 1, "strong_healing", 20.0),
)


def gear_probability(elapsed_seconds: float, config: PVPDropConfig) -> float:
    """Linearly decay gear probability from start to end over the configured window."""
    elapsed = max(0.0, float(elapsed_seconds))
    ratio = min(1.0, elapsed / config.probability_decay_seconds)
    return config.gear_probability_start + (
        config.gear_probability_end - config.gear_probability_start
    ) * ratio


def class_probabilities(
    elapsed_seconds: float,
    config: PVPDropConfig,
) -> dict[str, float]:
    """Return normalized item-class probabilities for one drop tick."""
    elapsed = max(0.0, float(elapsed_seconds))
    if elapsed >= config.food_healing_disabled_after_seconds:
        return {"ammo": 1.0}
    gear = gear_probability(elapsed, config)
    remainder = max(0.0, 1.0 - gear)
    share_sum = (
        config.ammo_share_before_disable
        + config.food_share_before_disable
        + config.healing_share_before_disable
    )
    raw = {
        "gear": gear,
        "ammo": remainder * config.ammo_share_before_disable / share_sum,
        "food": remainder * config.food_share_before_disable / share_sum,
        "healing": remainder * config.healing_share_before_disable / share_sum,
    }
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items() if value > 0}


def generate_drop_record(
    *,
    config: PVPDropConfig,
    rng: random.Random,
    spawn_index: int,
    elapsed_seconds: float,
    position: tuple[int, int, int] | None = None,
) -> PVPDropRecord:
    if spawn_index < 1:
        raise PVPSupplyError("spawn_index 必须 >= 1")
    probabilities = class_probabilities(elapsed_seconds, config)
    item_class = _weighted_choice(tuple(probabilities.items()), rng)
    resource, amount, profile = _choose_item(item_class, rng)
    if position is None:
        x, y, z = _random_drop_position(config.bounds, rng)
    else:
        x, y, z = position
    return PVPDropRecord(
        spawn_id=f"i{spawn_index:06d}",
        x=x,
        y=y,
        z=z,
        resource=resource,
        amount=amount,
        profile=profile,
        item_class=item_class,
        elapsed_seconds=max(0.0, float(elapsed_seconds)),
    )


def generate_drop_batch(
    *,
    config: PVPDropConfig,
    rng: random.Random,
    spawn_index_start: int,
    elapsed_seconds: float,
    count: int | None = None,
) -> list[PVPDropRecord]:
    """Generate one supply wave with spread-out positions."""
    if spawn_index_start < 1:
        raise PVPSupplyError("spawn_index_start 必须 >= 1")
    positions = generate_drop_positions(config=config, rng=rng, count=count)
    return [
        generate_drop_record(
            config=config,
            rng=rng,
            spawn_index=spawn_index_start + offset,
            elapsed_seconds=elapsed_seconds,
            position=position,
        )
        for offset, position in enumerate(positions)
    ]


def generate_drop_positions(
    *,
    config: PVPDropConfig,
    rng: random.Random,
    count: int | None = None,
) -> list[tuple[int, int, int]]:
    """Return random arena positions, preferring wide spacing within one wave."""
    target_count = config.drops_per_wave if count is None else int(count)
    if target_count < 1:
        raise PVPSupplyError("count 必须 >= 1")
    positions: list[tuple[int, int, int]] = []
    min_distance_sq = config.min_drop_spacing * config.min_drop_spacing
    for _ in range(target_count):
        positions.append(
            _best_drop_position(
                config.bounds,
                rng,
                positions,
                min_distance_sq=min_distance_sq,
                attempts=config.placement_attempts_per_drop,
            )
        )
    return positions


def summon_drop_command(record: PVPDropRecord, *, supply_tag: str) -> str:
    record_tag = f"ac_pvp_{record.spawn_id}"
    item_tag = _item_tag(record)
    item = (
        f'Item:{{id:"{record.resource}",Count:{record.amount}b'
        + (f",tag:{item_tag}" if item_tag else "")
        + "}"
    )
    nbt = (
        f'{{Tags:["{supply_tag}","{record_tag}"],Age:-32768s,'
        f"PickupDelay:0s,Motion:[0.0d,0.0d,0.0d],{item}}}"
    )
    return f"summon minecraft:item {record.x} {record.y} {record.z} {nbt}"


def _choose_item(item_class: str, rng: random.Random) -> tuple[str, int, str]:
    if item_class == "ammo":
        return "minecraft:arrow", 8, "default"
    if item_class == "food":
        return _weighted_choice(
            tuple(
                (resource, (amount, profile, weight))
                for resource, amount, profile, weight in _FOOD_ITEMS
            ),
            rng,
        )
    if item_class == "healing":
        return _weighted_choice(
            tuple(
                (resource, (amount, profile, weight))
                for resource, amount, profile, weight in _HEALING_ITEMS
            ),
            rng,
        )
    if item_class != "gear":
        raise PVPSupplyError(f"未知掉落类别: {item_class}")
    gear_kind = _weighted_choice(
        (("weapon", 45.0), ("armor", 40.0), ("bow", 15.0)),
        rng,
    )
    if gear_kind == "bow":
        return "minecraft:bow", 1, "default"
    if gear_kind == "weapon":
        material = _weighted_choice(_WEAPON_MATERIALS, rng)
        return f"minecraft:{material}_sword", 1, material
    material = _weighted_choice(_ARMOR_MATERIALS, rng)
    piece = _weighted_choice(tuple((item, 1.0) for item in _ARMOR_PIECES), rng)
    return f"minecraft:{material}_{piece}", 1, material


def _item_tag(record: PVPDropRecord) -> str | None:
    if record.resource != "minecraft:potion":
        return None
    potion = "minecraft:strong_healing" if record.profile == "strong_healing" else "minecraft:healing"
    return f'{{Potion:"{potion}"}}'


def _weighted_choice(
    values: Sequence[tuple[Any, float] | tuple[str, tuple[int, str, float]]],
    rng: random.Random,
) -> Any:
    if not values:
        raise PVPSupplyError("权重列表不能为空")
    entries: list[tuple[Any, float]] = []
    for key, value in values:
        if isinstance(value, tuple):
            amount, profile, weight = value
            entries.append(((key, amount, profile), float(weight)))
        else:
            entries.append((key, float(value)))
    total = sum(max(0.0, weight) for _, weight in entries)
    if total <= 0:
        raise PVPSupplyError("权重总和必须大于 0")
    threshold = rng.random() * total
    cumulative = 0.0
    for value, weight in entries:
        cumulative += max(0.0, weight)
        if threshold <= cumulative:
            return value
    return entries[-1][0]


def _best_drop_position(
    bounds: EncounterBounds,
    rng: random.Random,
    existing: Sequence[tuple[int, int, int]],
    *,
    min_distance_sq: float,
    attempts: int,
) -> tuple[int, int, int]:
    if not existing:
        return _random_drop_position(bounds, rng)
    best = _random_drop_position(bounds, rng)
    best_score = _nearest_distance_sq(best, existing)
    for _ in range(max(1, attempts) - 1):
        candidate = _random_drop_position(bounds, rng)
        score = _nearest_distance_sq(candidate, existing)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def _random_drop_position(
    bounds: EncounterBounds,
    rng: random.Random,
) -> tuple[int, int, int]:
    return (
        rng.randint(bounds.min_x, bounds.max_x),
        bounds.min_y,
        rng.randint(bounds.min_z, bounds.max_z),
    )


def _nearest_distance_sq(
    candidate: tuple[int, int, int],
    existing: Sequence[tuple[int, int, int]],
) -> float:
    return min(_distance_sq_2d(candidate, position) for position in existing)


def _distance_sq_2d(
    left: tuple[int, int, int],
    right: tuple[int, int, int],
) -> float:
    return float((left[0] - right[0]) ** 2 + (left[2] - right[2]) ** 2)


__all__ = [
    "PVPDropConfig",
    "PVPDropRecord",
    "PVPSupplyError",
    "PVP_SUPPLY_ITEM_NAMES",
    "class_probabilities",
    "gear_probability",
    "generate_drop_batch",
    "generate_drop_positions",
    "generate_drop_record",
    "summon_drop_command",
]
