from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_block,
    format_entity,
    format_item,
    result_ok,
    result_payload,
)


_BOSS_ENTITY_NAMES = frozenset({"wither", "ender_dragon"})


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "patrol":
        return describe_patrol(action_result, payload)
    if command == "observe":
        return describe_observe(action_result, payload)
    if command == "attack":
        return describe_attack(action_result, payload)
    if command == "retreat":
        return describe_retreat(action_result, payload)
    if command == "fight_using_weapon":
        return describe_fight_using_weapon(action_result, payload)
    if command == "fight_using_bow":
        return describe_fight_using_bow(action_result, payload)
    if command == "fight":
        return describe_fight(action_result, payload)
    if command == "use_food":
        return describe_use_food(action_result, payload)
    return None


def describe_patrol(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"巡逻感知失败：{error_reason(action_result, payload)}。"
    hostiles = payload.get("hostiles") or []
    chests = payload.get("chests") or []
    nearest_hostile = format_entity(hostiles[0]) if hostiles else "无"
    nearest_chest = format_block(chests[0]) if chests else "无"
    return f"巡逻感知完成：发现 {len(hostiles)} 个敌人，最近敌人为 {nearest_hostile}；发现 {len(chests)} 个箱子，最近箱子为 {nearest_chest}。"


def describe_observe(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    if not result_ok(action_result, payload):
        return f"侦查失败：{error_reason(action_result, payload)}。"
    radius = payload.get("radius")
    radius_text = f"（{round(radius, 1)} 格）" if isinstance(radius, (int, float)) else ""
    agents = payload.get("agents") or []
    hostiles = payload.get("hostiles") or []
    items = payload.get("items") or []
    agent_part = _observe_list_text(
        agents,
        label="agent",
        empty="未发现agent",
        renderer=_observe_entity_text,
    )
    hostile_part = _observe_list_text(
        hostiles,
        label="敌对实体",
        empty="未发现敌对实体",
        renderer=_observe_entity_text,
    )
    item_part = _observe_list_text(
        items,
        label="掉落物",
        empty="未发现掉落物",
        renderer=_observe_item_text,
    )
    return f"侦查完成：2倍感知半径{radius_text}内{agent_part}；{hostile_part}；{item_part}。"


def _observe_list_text(
    values: list[dict[str, Any]],
    *,
    label: str,
    empty: str,
    renderer: Any,
) -> str:
    if not values:
        return empty
    rendered = "、".join(renderer(item) for item in values)
    return f"发现 {len(values)} 个{label}：{rendered}"


def _observe_position(position: Any) -> str:
    """Render ``(x,y,z)`` with one decimal for floats."""
    if not isinstance(position, dict):
        return ""
    values = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, float):
            value = round(value, 1)
        values.append(str(value))
    return f"({','.join(values)})"


def _observe_entity_text(entity: dict[str, Any]) -> str:
    """Render a compact Boss name and an id-qualified ordinary entity."""
    name = entity.get("username") or entity.get("name") or entity.get("type") or "entity"
    name_key = str(name).split(":", 1)[-1].strip().lower()
    if name_key in _BOSS_ENTITY_NAMES or "boss" in (entity.get("roles") or []):
        entity_id = entity.get("id")
        if entity_id is None:
            entity_id = entity.get("entity_id")
        if isinstance(entity_id, bool) or not (
            isinstance(entity_id, int)
            or (isinstance(entity_id, str) and entity_id.strip().isdigit())
        ):
            entity_id = "boss"
        label = f"{name_key}_{entity_id}"
    else:
        entity_id = entity.get("id") or entity.get("entity_id")
        label = f"{name}_{entity_id}" if entity_id is not None else str(name)
    parts = [f"{label}{_observe_position(entity.get('position'))}"]
    distance = entity.get("distance")
    if isinstance(distance, (int, float)):
        parts.append(f"距离 {round(distance, 1)}")
    return "，".join(parts)


def _observe_item_text(item: dict[str, Any]) -> str:
    """Render ``物品×数量（位置）``."""
    name = item.get("name") or "unknown"
    count = item.get("count")
    text = f"{name}×{count}" if isinstance(count, (int, float)) else str(name)
    return f"{text}{_observe_position(item.get('position'))}"


def describe_retreat(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    nested = payload.get("result")
    if isinstance(nested, dict):
        payload = nested

    if not result_ok(action_result, payload):
        return f"撤退失败：{error_reason(action_result, payload)}。"

    target = format_entity(payload.get("target"))
    distance = payload.get("distance")
    requested = payload.get("requested_distance")
    distance_text = f"，当前距离 {round(distance, 2)}" if isinstance(distance, (int, float)) else ""
    requested_text = f"，目标距离 {round(requested, 2)}" if isinstance(requested, (int, float)) else ""

    if payload.get("reason") == "target_lost":
        return f"撤退结束：目标 {target} 已丢失{distance_text}。"
    if payload.get("retreated"):
        return f"撤退完成：已远离 {target}{distance_text}{requested_text}。"
    if payload.get("stopped"):
        return f"撤退已停止：目标 {target}{distance_text}，原因 {payload.get('reason', 'stopped')}。"
    return f"正在远离 {target}{distance_text}{requested_text}。"


def describe_food_update(payload: dict[str, Any]) -> str | None:
    food_result = payload.get("food_result")
    if not isinstance(food_result, dict):
        food_result = payload.get("last_food_result")
    if not isinstance(food_result, dict):
        return None

    food = food_result.get("food") or food_result.get("required_food") or "unknown"
    if not result_ok(food_result, food_result):
        return f"战斗中使用补给失败（{food}）：{error_reason(food_result, food_result)}。"

    consumed_text = "已使用补给" if food_result.get("consumed") else "尝试使用补给"
    parts = [f"战斗中{consumed_text} {food}"]
    hp_change = food_result.get("health_change")
    food_change = food_result.get("food_change")
    if isinstance(hp_change, (int, float)):
        parts.append(f"生命 {hp_change:+.1f}")
    if isinstance(food_change, (int, float)):
        parts.append(f"饱食度 {food_change:+.1f}")
    previous_weapon = food_result.get("previous_weapon") if isinstance(food_result.get("previous_weapon"), dict) else {}
    prev_name = previous_weapon.get("name")
    if prev_name:
        item_text = format_item(previous_weapon)
        if food_result.get("weapon_restored"):
            parts.append(f"已恢复手持 {item_text}")
        else:
            parts.append(f"恢复手持失败 {item_text}")
    return "，".join(parts) + "。"


def describe_collection(payload: dict[str, Any]) -> str:
    mode = payload.get("collect_mode")
    if mode is None:
        return ""
    if mode == "none":
        return "（未拾取掉落物）"
    count = payload.get("collected_count")
    if isinstance(count, (int, float)):
        return f"（已拾取 {int(count)} 个掉落物）" if count > 0 else "（未发现可拾取掉落物）"
    return "（掉落拾取已开启）"


def collect_failed_text(payload: dict[str, Any]) -> str | None:
    if payload.get("error_code") == "collect_failed" or payload.get("collection_failed"):
        return "目标已击败，但必须拾取的掉落物未能全部回收"
    return None


def combine_food_and_combat(food_text: str | None, combat_text: str) -> str:
    if not food_text:
        return combat_text
    return f"{food_text.rstrip('。')}；{combat_text}"


def fight_target_state(target: str, payload: dict[str, Any]) -> str:
    return f"{target}已被消灭" if payload.get("target_dead") else f"{target}依然存活"


def describe_fight_using_weapon(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    weapon = payload.get("weapon") or "unknown"
    food_text = describe_food_update(payload)
    if not result_ok(action_result, payload):
        collect_text = collect_failed_text(payload)
        if collect_text:
            return combine_food_and_combat(food_text, f"{collect_text}。")
        err = error_reason(action_result, payload)
        return combine_food_and_combat(food_text, f"武装战斗失败（{weapon}）：{err}。")
    target = format_entity(payload.get("target"))
    count = payload.get("attack_count", 0)
    if payload.get("target_dead"):
        duration = payload.get("duration_ms")
        duration_text = f"，耗时 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        return combine_food_and_combat(
            food_text,
            f"使用 {weapon} 击败 {target}，共攻击 {count} 次{duration_text}{describe_collection(payload)}。",
        )
    if payload.get("timeout"):
        duration = payload.get("duration_ms")
        duration_text = f"，已过 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        return combine_food_and_combat(food_text, f"武装战斗超时：使用 {weapon} 攻击 {target}，共攻击 {count} 次，目标未死亡{duration_text}。")
    if payload.get("stopped"):
        return combine_food_and_combat(food_text, f"武装战斗已停止：目标 {target}，共攻击 {count} 次，原因 {payload.get('reason', 'stopped')}。")
    distance = payload.get("last_distance")
    distance_text = f"，距离 {round(distance, 2)}" if isinstance(distance, (int, float)) else ""
    return combine_food_and_combat(food_text, f"使用 {weapon} 攻击 {target}，已攻击 {count} 次{distance_text}。")


def describe_use_food(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    food = payload.get("food") or payload.get("required_food") or "unknown"
    if not result_ok(action_result, payload):
        err = error_reason(action_result, payload)
        return f"使用补给失败（{food}）：{err}。"
    consumed = "已使用补给" if payload.get("consumed") else "尝试使用补给"
    hp_change = payload.get("health_change")
    food_change = payload.get("food_change")
    parts = [f"{consumed} {food}"]
    if isinstance(hp_change, (int, float)):
        parts.append(f"生命 {hp_change:+.1f}")
    if isinstance(food_change, (int, float)):
        parts.append(f"饱食度 {food_change:+.1f}")
    weapon_restored = payload.get("weapon_restored")
    prev_weapon = (payload.get("previous_weapon") or {}).get("name")
    if prev_weapon:
        parts.append(f"武器已恢复 {prev_weapon}" if weapon_restored else f"武器恢复失败 {prev_weapon}")
    return "，".join(parts) + "。"


def describe_fight(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    weapon = payload.get("weapon") or "unknown"
    combat_type = payload.get("combat_type", "melee")
    food_text = describe_food_update(payload)
    if not result_ok(action_result, payload):
        collect_text = collect_failed_text(payload)
        if collect_text:
            return combine_food_and_combat(food_text, f"{collect_text}。")
        err = error_reason(action_result, payload)
        return combine_food_and_combat(food_text, f"战斗失败（{weapon}）：{err}。")
    target = format_entity(payload.get("target"))
    target_state = fight_target_state(target, payload)
    if payload.get("target_dead"):
        duration = payload.get("duration_ms")
        duration_text = f"，耗时 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        if combat_type == "throwable":
            shots = payload.get("shots_fired", 0)
            return combine_food_and_combat(
                food_text,
                f"使用 {weapon} 投掷攻击，{target_state}，投掷 {shots} 次{duration_text}{describe_collection(payload)}。",
            )
        elif combat_type == "ranged":
            shots = payload.get("shots_fired", 0)
            return combine_food_and_combat(
                food_text,
                f"使用 {weapon} 进行远程攻击，{target_state}，射击 {shots} 次{duration_text}{describe_collection(payload)}。",
            )
        else:
            count = payload.get("attack_count", 0)
            return combine_food_and_combat(
                food_text,
                f"使用 {weapon} 攻击，{target_state}，攻击 {count} 次{duration_text}{describe_collection(payload)}。",
            )
    if payload.get("out_of_ammo"):
        shots = payload.get("shots_fired", 0)
        if combat_type == "throwable":
            return combine_food_and_combat(food_text, f"投掷战斗中断：投掷物耗尽，已投掷 {shots} 次，{target_state}。")
        return combine_food_and_combat(food_text, f"弓箭战斗中断：箭矢耗尽，已射击 {shots} 次，{target_state}。")
    if payload.get("timeout"):
        duration = payload.get("duration_ms")
        duration_text = f"，已过 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        return combine_food_and_combat(food_text, f"战斗超时（{weapon}）：{target_state}{duration_text}。")
    if payload.get("stopped"):
        return combine_food_and_combat(food_text, f"战斗已停止：{target}（{weapon}），{target_state}，原因 {payload.get('reason', 'stopped')}。")
    distance = payload.get("last_distance")
    distance_text = f"，距离 {round(distance, 1)}" if isinstance(distance, (int, float)) else ""
    if combat_type == "ranged":
        shots = payload.get("shots_fired", 0)
        arrows = payload.get("arrows_remaining")
        ammo_text = f"，剩余 {arrows} 支箭" if isinstance(arrows, (int, float)) else ""
        phase = payload.get("phase", "")
        phase_map = {"approaching": "接近中", "aiming": "瞄准中", "charging": "蓄力中", "releasing": "装填中"}
        phase_text = f"【{phase_map.get(phase, phase)}】" if phase else ""
        return combine_food_and_combat(food_text, f"使用 {weapon}{phase_text} 攻击，{target_state}，已射击 {shots} 次{ammo_text}{distance_text}。")
    elif combat_type == "throwable":
        shots = payload.get("shots_fired", 0)
        ammo = payload.get("ammo_remaining")
        ammo_text = f"，剩余 {ammo} 个投掷物" if isinstance(ammo, (int, float)) else ""
        phase = payload.get("phase", "")
        phase_map = {"approaching": "接近中", "aiming": "瞄准中", "throwing": "投掷中", "cooldown": "冷却中"}
        phase_text = f"【{phase_map.get(phase, phase)}】" if phase else ""
        return combine_food_and_combat(food_text, f"使用 {weapon}{phase_text} 投掷攻击，{target_state}，已投掷 {shots} 次{ammo_text}{distance_text}。")
    else:
        count = payload.get("attack_count", 0)
        return combine_food_and_combat(food_text, f"使用 {weapon} 攻击，{target_state}，已攻击 {count} 次{distance_text}。")


def describe_fight_using_bow(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    bow = payload.get("bow") or "unknown"
    food_text = describe_food_update(payload)
    if not result_ok(action_result, payload):
        err = error_reason(action_result, payload)
        return combine_food_and_combat(food_text, f"弓箭战斗失败（{bow}）：{err}。")
    target = format_entity(payload.get("target"))
    shots = payload.get("shots_fired", 0)
    arrows_left = payload.get("arrows_remaining")
    ammo_text = f"，剩余 {arrows_left} 支箭" if isinstance(arrows_left, (int, float)) else ""
    if payload.get("target_dead"):
        duration = payload.get("duration_ms")
        duration_text = f"，耗时 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        return combine_food_and_combat(food_text, f"使用 {bow} 射杀 {target}，射击 {shots} 次{ammo_text}{duration_text}。")
    if payload.get("out_of_ammo"):
        return combine_food_and_combat(food_text, f"弓箭战斗中断：箭矢耗尽，已射击 {shots} 次，目标 {target} 未死亡。")
    if payload.get("timeout"):
        duration = payload.get("duration_ms")
        duration_text = f"，已过 {round(duration / 1000, 1)}s" if isinstance(duration, (int, float)) else ""
        return combine_food_and_combat(food_text, f"弓箭战斗超时：使用 {bow} 攻击 {target}，射击 {shots} 次，目标未死亡{duration_text}。")
    if payload.get("stopped"):
        return combine_food_and_combat(food_text, f"弓箭战斗已停止：目标 {target}，射击 {shots} 次{ammo_text}。")
    distance = payload.get("last_distance")
    phase = payload.get("phase", "")
    phase_map = {"approaching": "接近中", "aiming": "瞄准中", "charging": "蓄力中", "releasing": "装填中"}
    phase_text = f"【{phase_map.get(phase, phase)}】" if phase else ""
    distance_text = f"，距离 {round(distance, 1)}" if isinstance(distance, (int, float)) else ""
    return combine_food_and_combat(food_text, f"使用 {bow}{phase_text} 攻击 {target}，已射击 {shots} 次{ammo_text}{distance_text}。")


def describe_attack(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    food_text = describe_food_update(payload)
    if not result_ok(action_result, payload):
        collect_text = collect_failed_text(payload)
        if collect_text:
            return combine_food_and_combat(food_text, f"{collect_text}。")
        return combine_food_and_combat(food_text, f"攻击失败：{error_reason(action_result, payload)}。")
    target = format_entity(payload.get("target"))
    count = payload.get("attack_count", 0)
    if payload.get("target_dead"):
        return combine_food_and_combat(
            food_text,
            f"攻击完成：目标 {target} 已死亡，共攻击 {count} 次{describe_collection(payload)}。",
        )
    if payload.get("stopped"):
        return combine_food_and_combat(food_text, f"攻击已停止：目标 {target}，共攻击 {count} 次，原因 {payload.get('reason', 'stopped')}。")
    distance = payload.get("last_distance")
    distance_text = f"，最后距离 {round(distance, 2)}" if isinstance(distance, (int, float)) else ""
    return combine_food_and_combat(food_text, f"正在攻击 {target}，已攻击 {count} 次，目标未死亡{distance_text}。")
