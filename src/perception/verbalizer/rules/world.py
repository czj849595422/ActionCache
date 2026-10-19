"""Terminal text rendering for task-independent world facts (W01-W16)."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from ..base import VerbalizeContext, fmt_number


WORLD_SECTION_TITLES: dict[str, tuple[str, str]] = {
    "self_state": ("自身", "Self"),
    "held_and_equipment": ("手持与装备", "Held and Equipment"),
    "inventory": ("背包", "Inventory"),
    "players": ("其他玩家", "Other Players"),
    "hostile_entities": ("敌对实体", "Hostile Entities"),
    "passive_neutral_entities": ("其他生物", "Other Creatures"),
    "projectiles_explosives": ("投射物与爆炸物", "Projectiles and Explosives"),
    "other_entities": ("其他实体", "Other Entities"),
    "dropped_items": ("附近掉落物", "Nearby Dropped Items"),
    "nearby_blocks": ("附近方块", "Nearby Blocks"),
    "resources": ("可采集资源", "Harvestable Resources"),
    "containers": ("容器或商店", "Containers or Shops"),
    "processors": ("加工设备", "Processors"),
    "workstations": ("工作站", "Workstations"),
    "world_environment": ("环境", "Environment"),
    "observation_coverage": ("感知范围", "Observation Coverage"),
}

_EMPTY_ZH = {
    "players": "范围内未发现其他玩家。",
    "hostile_entities": "范围内未发现敌对实体。",
    "passive_neutral_entities": "范围内未发现其他生物。",
    "projectiles_explosives": "范围内未发现投射物或爆炸物。",
    "other_entities": "范围内未发现其他实体。",
    "dropped_items": "范围内未发现掉落物。",
    "nearby_blocks": "范围内未发现非空气方块。",
    "resources": "范围内未发现可采集资源。",
    "containers": "范围内未发现容器。",
    "processors": "范围内未发现加工设备。",
    "workstations": "范围内未发现工作站。",
}

_DIRECTION_ZH = {
    "front": "前方",
    "front_left": "左前方",
    "left": "左侧",
    "back_left": "左后方",
    "back": "后方",
    "back_right": "右后方",
    "right": "右侧",
    "front_right": "右前方",
    "above": "上方",
    "below": "下方",
    "same_position": "同一位置",
}

_DAY_PHASE_ZH = {"dawn": "黎明", "day": "白天", "dusk": "黄昏", "night": "夜晚"}
_WEATHER_ZH = {"clear": "晴朗", "rain": "下雨", "thunder": "雷暴"}
_DIFFICULTY_ZH = {
    "peaceful": "和平",
    "easy": "简单",
    "normal": "普通",
    "hard": "困难",
    0: "和平",
    1: "简单",
    2: "普通",
    3: "困难",
}


@dataclass(frozen=True)
class WorldSectionRule:
    """Render one already-filtered world fact section without changing it."""

    section_name: str
    limit: int | None = None
    show_disabled: bool = False

    def __post_init__(self) -> None:
        if self.section_name not in WORLD_SECTION_TITLES:
            raise ValueError(f"unknown world section: {self.section_name}")
        if self.limit is not None and self.limit < 0:
            raise ValueError("world section limit must be non-negative")

    def render(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        sections = observation.get("sections") if isinstance(observation, dict) else None
        if not isinstance(sections, dict):
            return None
        section = sections.get(self.section_name)
        if not isinstance(section, dict):
            return None
        status = section.get("status")
        is_pvp_players = self.section_name == "players" and observation.get("category") == "pvp"
        if status == "disabled" and not self.show_disabled:
            return None

        title = WORLD_SECTION_TITLES[self.section_name][context.language == "en"]
        prefix = f"[{title}]" if context.language == "en" else f"【{title}】"
        if status == "unknown":
            reason = section.get("reason")
            if context.language == "en":
                body = "Perception failed; current state is unknown."
                if reason:
                    body += f" Reason: {reason}."
            else:
                body = f"{title}感知失败，当前状态未知。"
                if reason:
                    body += f"原因：{reason}。"
            return f"{prefix}\n{body}"
        if status == "disabled":
            body = "Enhancement is disabled." if context.language == "en" else "增强感知未启用。"
            return f"{prefix}\n{body}"
        if status == "empty":
            if is_pvp_players:
                return f"{prefix}\n" + "\n".join(_render_pvp_players([], context.language))
            body = _empty_text(self.section_name, context.language)
            return f"{prefix}\n{body}"

        facts = [item for item in section.get("facts", []) if isinstance(item, dict)]
        visible = facts if self.limit is None else facts[: self.limit]
        if is_pvp_players or (
            self.section_name == "players" and _has_pvp_player_relations(facts)
        ):
            lines = _render_pvp_players(visible, context.language)
        else:
            lines = [
                rendered
                for fact in visible
                if (rendered := _render_fact(self.section_name, fact, context.language))
            ]
        omitted = max(0, len(facts) - len(visible))
        if omitted:
            lines.append(
                f"{omitted} more objects omitted."
                if context.language == "en"
                else f"另有 {omitted} 个对象未展开。"
            )
        if not lines:
            lines.append("Observed; no renderable facts." if context.language == "en" else "已观察，暂无可渲染事实。")
        return f"{prefix}\n" + "\n".join(lines)


def _empty_text(section_name: str, language: str) -> str:
    if language == "zh":
        return _EMPTY_ZH.get(section_name, "已完成观察，但没有事实。")
    title = WORLD_SECTION_TITLES[section_name][1].lower()
    return f"No {title} were observed in range."


def _render_fact(section: str, fact: dict[str, Any], language: str) -> str:
    renderer = {
        "self_state": _render_self,
        "held_and_equipment": _render_equipment,
        "inventory": _render_inventory,
        "players": _render_player,
        "hostile_entities": _render_hostile,
        "passive_neutral_entities": _render_creature,
        "projectiles_explosives": _render_projectile,
        "other_entities": _render_other_entity,
        "dropped_items": _render_dropped_item,
        "nearby_blocks": _render_block,
        "resources": _render_resource,
        "containers": _render_container,
        "processors": _render_processor,
        "workstations": _render_workstation,
        "world_environment": _render_environment,
        "observation_coverage": _render_coverage,
    }[section]
    return renderer(fact, language)


def _render_self(fact: dict[str, Any], language: str) -> str:
    name = str(fact.get("agent_name") or "agent")
    parts: list[str] = []
    health = _health_text(
        fact.get("health"),
        fact.get("max_health"),
        fact.get("health_delta"),
        language,
    )
    food = _ratio(fact.get("food"), fact.get("max_food"))
    oxygen = _ratio(fact.get("oxygen"), fact.get("max_oxygen"))
    if language == "en":
        if health:
            parts.append(f"health {health}")
        if food:
            parts.append(f"food {food}")
        if oxygen:
            parts.append(f"oxygen {oxygen}")
        if fact.get("recent_attack_count") is not None:
            parts.append(f"recent attacks {fact['recent_attack_count']}")
        parts.extend(_position_and_direction_parts(fact, language))
        states = _self_states(fact, language)
        parts.append("status " + (", ".join(states) if states else "normal"))
        if fact.get("persistent_damage_state"):
            parts.append("currently under sustained damage pressure")
    else:
        if health:
            parts.append(f"生命值 {health}")
        if food:
            parts.append(f"饥饿值 {food}")
        if oxygen:
            parts.append(f"氧气值 {oxygen}")
        parts.extend(_position_and_direction_parts(fact, language))
        if fact.get("recent_attack_count") is not None:
            parts.append(f"近期受到攻击次数：{fact['recent_attack_count']}次")
        states = _self_states(fact, language)
        parts.append("状态：" + ("、".join(states) if states else "正常"))
        if fact.get("persistent_damage_state"):
            parts.append("当前处于持续受伤状态")
    rendered = _join(parts, language)
    if not parts:
        return name
    separator = ": " if language == "en" else "："
    return name + separator + rendered


def _render_equipment(fact: dict[str, Any], language: str) -> str:
    slots = (
        (("main_hand", "main hand", "主手"), ("off_hand", "off hand", "副手"),
         ("head", "head", "头部"), ("chest", "chest", "胸部"),
         ("legs", "legs", "腿部"), ("feet", "feet", "脚部"))
    )
    parts = []
    for key, en_label, zh_label in slots:
        label = en_label if language == "en" else zh_label
        parts.append(
            f"{label}: {_item_text(fact.get(key), language, include_count=False)}"
        )
    quick = fact.get("quick_bar_slot")
    if quick is not None:
        parts.append(f"quick bar slot {quick}" if language == "en" else f"快捷栏槽位 {quick}")
    return _join(parts, language)


def _render_inventory(fact: dict[str, Any], language: str) -> str:
    items = [_item_text(item, language) for item in fact.get("totals", []) if isinstance(item, dict)]
    if not items:
        items.append("empty" if language == "en" else "空")
    empty = fact.get("empty_slots")
    total = fact.get("total_slots")
    if empty is not None and total is not None:
        items.append(f"empty slots {empty}/{total}" if language == "en" else f"空槽 {empty}/{total}")
    return _join(items, language)


def _render_player(fact: dict[str, Any], language: str) -> str:
    parts = [str(fact.get("player_name") or _entity_label(fact))]
    relation = fact.get("relation")
    team = fact.get("team")
    if relation or team:
        if language == "en":
            relation_text = relation or "unknown"
            parts.append(f"relation: {relation_text}; team: {team or 'unknown'}")
        else:
            relation_text = {
                "self": "自己",
                "friendly": "友方",
                "enemy": "敌方",
            }.get(relation, relation or "未知")
            parts.append(f"关系：{relation_text}；队伍：{team or '未知'}")
    parts.extend(_position_and_direction_parts(fact, language, include_direction=True))
    if fact.get("held_item"):
        parts.append(
            f"held {_item_text(fact['held_item'], language, include_count=False, include_durability=False)}"
            if language == "en"
            else f"手持 {_item_text(fact['held_item'], language, include_count=False, include_durability=False)}"
        )
    return _join(parts, language)


def _has_pvp_player_relations(facts: list[dict[str, Any]]) -> bool:
    return any(fact.get("relation") in {"friendly", "enemy", "self"} for fact in facts)


def _render_pvp_players(facts: list[dict[str, Any]], language: str) -> list[str]:
    if language == "en":
        labels = (("friendly", "Teammates:"), ("enemy", "Enemies:"))
        empty = "none"
    else:
        labels = (("friendly", "队友："), ("enemy", "敌方："))
        empty = "无"
    lines: list[str] = []
    for relation, label in labels:
        lines.append(label)
        grouped = [fact for fact in facts if fact.get("relation") == relation]
        if not grouped:
            lines.append(empty)
            continue
        lines.extend(_render_pvp_player_line(fact, language) for fact in grouped)
    return lines


def _render_pvp_player_line(fact: dict[str, Any], language: str) -> str:
    name = str(fact.get("player_name") or _entity_label(fact))
    position = _position_text(fact.get("position"), integer=False)
    distance_direction = _pvp_distance_direction_text(fact, language)
    equipment = _pvp_player_equipment_text(fact, language)
    if language == "en":
        return f"{name}: {position}, {distance_direction}, {equipment}"
    return f"{name}：{position}，{distance_direction}，{equipment}"


def _pvp_distance_direction_text(fact: dict[str, Any], language: str) -> str:
    distance = _distance_text(fact.get("distance"), language)
    direction = _direction_text(fact.get("relative_direction"), language)
    if language == "en":
        if distance and direction:
            return f"{distance}, direction: {direction}"
        return distance or (f"direction: {direction}" if direction else "unknown distance/direction")
    if distance and direction:
        return f"{distance}，方向：{direction}"
    return distance or (f"方向：{direction}" if direction else "距离方位未知")


def _pvp_player_equipment_text(fact: dict[str, Any], language: str) -> str:
    parts: list[str] = []
    held = fact.get("held_item")
    if held:
        brief = _item_text(held, language, include_count=False, include_durability=False)
        parts.append(f"held {brief}" if language == "en" else f"手持 {brief}")
    equipment = fact.get("equipment")
    slot_labels = (
        ("off_hand", "off hand", "副手"),
        ("head", "head", "头部"),
        ("chest", "chest", "胸部"),
        ("legs", "legs", "腿部"),
        ("feet", "feet", "脚部"),
    )
    if isinstance(equipment, dict):
        for key, en_label, zh_label in slot_labels:
            item = equipment.get(key)
            if item:
                brief = _item_text(item, language, include_count=False, include_durability=False)
                label = en_label if language == "en" else zh_label
                parts.append(f"{label} {brief}")
    if language == "en":
        return "equipped: " + (", ".join(parts) if parts else "none")
    return "已装备：" + ("、".join(parts) if parts else "无")


def _render_hostile(fact: dict[str, Any], language: str) -> str:
    parts = [_entity_label(fact)]
    if "boss" in (fact.get("roles") or []):
        parts.append("role: Boss" if language == "en" else "角色：Boss")
        current = fact.get("health")
        maximum = fact.get("max_health")
        if current is not None or maximum is not None:
            health = _unknown_number(current, language)
            maximum_text = _unknown_number(maximum, language)
            parts.append(
                f"health {health}/{maximum_text}"
                if language == "en"
                else f"血量：{health}/{maximum_text}"
            )
    parts.extend(_position_and_direction_parts(fact, language, include_direction=True))
    _append_entity_loadout(parts, fact, language)
    return _join(parts, language)


def _render_creature(fact: dict[str, Any], language: str) -> str:
    parts = [_entity_label(fact)]
    entity_class = fact.get("entity_class")
    class_zh = {"passive": "被动", "neutral": "中立", "utility": "功能型"}.get(entity_class, entity_class)
    if entity_class:
        parts.append(f"type: {entity_class}" if language == "en" else f"类型：{class_zh}")
    parts.extend(_position_and_direction_parts(fact, language, include_direction=True))
    return _join(parts, language)


def _render_projectile(fact: dict[str, Any], language: str) -> str:
    parts = [_entity_label(fact)]
    parts.extend(_position_and_direction_parts(fact, language, include_direction=True))
    if fact.get("moving_toward_self") is True:
        parts.append("moving toward self" if language == "en" else "正在接近")
    fuse = fact.get("fuse_ticks")
    if fuse is not None:
        parts.append(f"fuse {fuse} ticks" if language == "en" else f"剩余引信 {fuse} tick")
    return _join(parts, language)


def _render_other_entity(fact: dict[str, Any], language: str) -> str:
    parts = [_entity_label(fact)]
    parts.extend(_position_and_direction_parts(fact, language))
    return _join(parts, language)


def _render_dropped_item(fact: dict[str, Any], language: str) -> str:
    parts = [_item_text(fact.get("item"), language)]
    parts.extend(_position_and_direction_parts(fact, language))
    return _join(parts, language)


def _render_block(fact: dict[str, Any], language: str) -> str:
    if fact.get("is_ore_group"):
        resource = str(fact.get("resource_name") or fact.get("block_name") or "unknown")
        count = fact.get("block_count", "?")
        position = _position_text(fact.get("position"), integer=True)
        distance = _distance_text(fact.get("distance"), language)
        if language == "en":
            suffix = f", {distance}" if distance else ""
            return f"{resource} group ({count} blocks) at {position}{suffix}"
        suffix = f"，{distance}" if distance else ""
        return f"{resource} 矿物组（{count}个方块）位于 {position}{suffix}"
    label = f"{fact.get('block_name', 'unknown')}_{_position_text(fact.get('position'), integer=True)}"
    parts = [label]
    distance = _distance_text(fact.get("distance"), language)
    if distance:
        parts.append(distance)
    if fact.get("diggable") is not None:
        if language == "en":
            parts.append(f"diggable: {'yes' if fact['diggable'] else 'no'}")
        else:
            parts.append(f"可挖掘：{'是' if fact['diggable'] else '否'}")
    properties = fact.get("properties")
    if isinstance(properties, dict) and properties:
        state = ", ".join(f"{key}={value}" for key, value in sorted(properties.items()))
        parts.append(f"state: {state}" if language == "en" else f"状态：{state}")
    return _join(parts, language)


def _render_resource(fact: dict[str, Any], language: str) -> str:
    count = fact.get("block_count")
    parts = [str(fact.get("resource_name") or "unknown")]
    if language == "en":
        parts.append(f"{count} blocks in this connected group")
        parts.append(f"nearest {_position_text(fact.get('nearest_position'), integer=True)}")
    else:
        parts.append(f"本组发现 {count} 个面相邻方块")
        parts.append(f"最近位置 {_position_text(fact.get('nearest_position'), integer=True)}")
    distance = _distance_text(fact.get("nearest_distance"), language, nearest=True)
    if distance:
        parts.append(distance)
    tools = fact.get("harvest_tools") or []
    if tools:
        text = "、".join(map(str, tools)) if language == "zh" else ", ".join(map(str, tools))
        parts.append(f"required tools: {text}" if language == "en" else f"工具要求：{text}")
    if fact.get("growth_stage") is not None:
        mature = fact.get("mature")
        parts.append(
            f"growth stage {fact['growth_stage']}, mature={mature}"
            if language == "en"
            else f"生长阶段 {fact['growth_stage']}，{'已成熟' if mature else '未成熟'}"
        )
    return _join(parts, language)


def _render_container(fact: dict[str, Any], language: str) -> str:
    label = fact.get("registered_name") or (
        f"{fact.get('container_type', 'container')}_"
        f"{_position_text(fact.get('position'), integer=True)}"
    )
    parts = [label]
    role = fact.get("container_role")
    owner = fact.get("owner")
    if role:
        if language == "en":
            parts.append(f"role: {role}; owner: {owner}")
        else:
            role_name = "商铺" if role == "shop" else "私人箱"
            parts.append(f"用途：{role_name}；所有者：{owner}")
    distance = _distance_text(fact.get("distance"), language)
    if distance:
        parts.append(distance)
    status = fact.get("internal_status")
    if status == "observed":
        totals = fact.get("sale_totals") if role == "shop" else fact.get("totals")
        totals = totals or []
        contents = [_item_text(item, language) for item in totals if isinstance(item, dict)]
        content_text = (", ".join(contents) if language == "en" else "、".join(contents)) or (
            "empty" if language == "en" else "空"
        )
        if fact.get("freshness") == "cached":
            age = (fact.get("age_ms") or 0) / 1000
            parts.append(
                f"contents from {fmt_number(age)} seconds ago: {content_text}"
                if language == "en"
                else f"内容来自 {fmt_number(age)} 秒前的缓存：{content_text}"
            )
        else:
            parts.append(f"contents: {content_text}" if language == "en" else f"内容：{content_text}")
        if fact.get("empty_slots") is not None and fact.get("total_slots") is not None:
            parts.append(
                f"empty slots {fact['empty_slots']}/{fact['total_slots']}"
                if language == "en"
                else f"空槽 {fact['empty_slots']}/{fact['total_slots']}"
            )
    else:
        parts.append(_internal_status_text(status, fact.get("reason"), language))
    return _join(parts, language)


def _render_processor(fact: dict[str, Any], language: str) -> str:
    label = f"{fact.get('processor_type', 'processor')}_{_position_text(fact.get('position'), integer=True)}"
    parts = [label]
    distance = _distance_text(fact.get("distance"), language)
    if distance:
        parts.append(distance)
    lit = fact.get("lit")
    if lit is not None:
        parts.append(
            ("burning" if lit else "not burning")
            if language == "en"
            else ("正在燃烧" if lit else "未点燃")
        )
    if fact.get("internal_status") != "observed":
        parts.append(_internal_status_text(fact.get("internal_status"), fact.get("reason"), language))
        return _join(parts, language)
    for key, en_label, zh_label in (
        ("input", "input", "输入"),
        ("fuel", "fuel", "燃料"),
        ("output", "output", "输出"),
    ):
        if fact.get(key):
            parts.append(
                f"{en_label} {_item_text(fact[key], language)}"
                if language == "en"
                else f"{zh_label} {_item_text(fact[key], language)}"
            )
    progress = fact.get("progress_ratio")
    if isinstance(progress, (int, float)):
        percent = max(0, min(100, round(float(progress) * 100)))
        parts.append(f"progress {percent}%" if language == "en" else f"处理进度 {percent}%")
    return _join(parts, language)


def _render_workstation(fact: dict[str, Any], language: str) -> str:
    workstation_type = fact.get("workstation_type", "workstation")
    position = _position_text(fact.get("position"), integer=True)
    distance = _distance_text(fact.get("distance"), language)
    direction = _direction_text(fact.get("relative_direction"), language)
    if language == "zh":
        text = f"{workstation_type}：位置 {position}"
        if distance:
            text += f"，{distance}"
        if direction:
            text += f"；位于{direction}"
        return text + "。"
    parts = [f"{workstation_type}: position {position}"]
    if distance:
        parts.append(distance)
    if direction:
        parts.append(f"direction: {direction}")
    if fact.get("within_use_range"):
        parts.append("interactable")
    return _join(parts, language)


def _render_environment(fact: dict[str, Any], language: str) -> str:
    parts: list[str] = []
    dimension = fact.get("dimension")
    if dimension:
        dimension_text = {"overworld": "主世界", "the_nether": "下界", "the_end": "末地"}.get(
            str(dimension).split(":")[-1], str(dimension)
        )
        parts.append(str(dimension) if language == "en" else dimension_text)
    phase = fact.get("day_phase")
    if phase:
        parts.append(str(phase) if language == "en" else _DAY_PHASE_ZH.get(phase, str(phase)))
    weather = fact.get("weather")
    if weather:
        parts.append(f"weather {weather}" if language == "en" else f"天气{_WEATHER_ZH.get(weather, weather)}")
    difficulty = fact.get("difficulty")
    if difficulty is not None:
        parts.append(
            f"difficulty {difficulty}"
            if language == "en"
            else f"{_DIFFICULTY_ZH.get(difficulty, difficulty)}难度"
        )
    if fact.get("light") is not None:
        parts.append(f"light {fact['light']}" if language == "en" else f"当前位置光照 {fact['light']}")
    if fact.get("biome") is not None:
        parts.append(f"biome {fact['biome']}" if language == "en" else f"生物群系 {fact['biome']}")
    return _join(parts, language)


def _render_coverage(fact: dict[str, Any], language: str) -> str:
    parts: list[str] = []
    mode = fact.get("mode")
    sub_mode = fact.get("sub_mode")
    if language == "en":
        parts.append(f"{mode or 'unknown'} perception ({sub_mode or 'unknown'})")
    else:
        label = "局部" if mode == "local" else "客户端已加载范围" if mode == "global" else "未知"
        parts.append(f"{label}{'视野' if sub_mode == 'view' else '半径'}感知")
    if fact.get("radius") is not None:
        parts.append(f"entity radius {fmt_number(fact['radius'])} blocks" if language == "en" else f"实体半径 {fmt_number(fact['radius'])} 格")
    if fact.get("block_radius") is not None:
        parts.append(f"block radius {fmt_number(fact['block_radius'])} blocks" if language == "en" else f"方块半径 {fmt_number(fact['block_radius'])} 格")
    if fact.get("scanned_blocks") is not None:
        parts.append(f"scanned {fact['scanned_blocks']} block positions" if language == "en" else f"扫描 {fact['scanned_blocks']} 个方块位置")
    if fact.get("unloaded_blocks") is not None:
        parts.append(f"{fact['unloaded_blocks']} unloaded" if language == "en" else f"{fact['unloaded_blocks']} 个位置未加载")
    if fact.get("observed_at_ms") is not None:
        parts.append(f"timestamp {fact['observed_at_ms']}" if language == "en" else f"观察时间戳 {fact['observed_at_ms']}")
    errors = fact.get("enhancement_errors") or []
    if errors:
        parts.append(f"{len(errors)} enhancement errors" if language == "en" else f"增强查询错误 {len(errors)} 个")
    return _join(parts, language)


def _position_and_direction_parts(
    fact: dict[str, Any], language: str, *, include_direction: bool = False
) -> list[str]:
    parts: list[str] = []
    if fact.get("position") is not None:
        position = _position_text(fact.get("position"), integer=False)
        parts.append(f"position {position}" if language == "en" else f"位置 {position}")
    distance = _distance_text(fact.get("distance"), language)
    if distance:
        parts.append(distance)
    if include_direction:
        direction = _direction_text(fact.get("relative_direction"), language)
        if direction:
            parts.append(f"direction: {direction}" if language == "en" else f"方向：{direction}")
    return parts


def _append_entity_loadout(parts: list[str], fact: dict[str, Any], language: str) -> None:
    held = fact.get("held_item")
    if held:
        brief = _item_text(held, language, include_count=False, include_durability=False)
        parts.append(f"held {brief}" if language == "en" else f"手持 {brief}")
    equipment = fact.get("equipment")
    if isinstance(equipment, dict):
        items = [
            _item_text(item, language, include_count=False, include_durability=False)
            for item in equipment.values()
            if item
        ]
        if items:
            joined = ", ".join(items) if language == "en" else "、".join(items)
            parts.append(f"equipment {joined}" if language == "en" else f"装备 {joined}")


def _self_states(fact: dict[str, Any], language: str) -> list[str]:
    values = []
    flags = (
        ("burning", "burning", "燃烧"),
        ("in_water", "in water", "位于水中"),
        ("in_lava", "in lava", "位于岩浆中"),
        ("sleeping", "sleeping", "睡眠"),
        ("on_ground", "on ground", "位于地面"),
    )
    for key, en_text, zh_text in flags:
        if fact.get(key):
            values.append(en_text if language == "en" else zh_text)
    effects = fact.get("effects")
    if isinstance(effects, dict):
        values.extend(str(value.get("name") or key) if isinstance(value, dict) else str(key) for key, value in effects.items())
    elif isinstance(effects, list):
        values.extend(str(value.get("name") or value.get("id")) for value in effects if isinstance(value, dict))
    return values


def _item_text(
    item: Any,
    language: str,
    *,
    include_count: bool = True,
    include_durability: bool = True,
) -> str:
    if not isinstance(item, dict) or not item.get("name"):
        return "none" if language == "en" else "无"
    identifier = item.get("identifier")
    name = str(identifier or item["name"])
    potion = _find_nested(item.get("nbt"), "Potion")
    if isinstance(potion, str) and not identifier:
        name += f"[{potion.split(':')[-1]}]"
    if identifier:
        enchantments = item.get("enchantments") or []
        enchantment_text = []
        for entry in enchantments:
            if not isinstance(entry, dict):
                continue
            enchantment_id = entry.get("id")
            level = entry.get("level")
            if enchantment_id:
                enchantment_text.append(
                    f"{enchantment_id} {level}" if level is not None else str(enchantment_id)
                )
        label = ", ".join(enchantment_text) if language == "en" else "、".join(enchantment_text)
        if label:
            name = f"{name} ({label})"
    text = name
    if include_count:
        count = item.get("count")
        if count is not None:
            text = f"{name}×{count}"
    if include_durability:
        durability = item.get("durability")
        maximum = item.get("max_durability")
        if durability is not None and maximum is not None:
            text += f" ({'durability' if language == 'en' else '耐久'} {durability}/{maximum})"
    return text


def _find_nested(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_nested(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested(child, key)
            if found is not None:
                return found
    return None


def _entity_label(fact: dict[str, Any]) -> str:
    kind = str(fact.get("kind") or "entity")
    if "boss" in (fact.get("roles") or []):
        entity_id = fact.get("entity_id")
        if isinstance(entity_id, bool):
            entity_id = None
        elif not isinstance(entity_id, int):
            entity_id = str(entity_id or "").strip()
            if not entity_id.isdigit():
                entity_id = None
        return f"{kind}_{entity_id if entity_id is not None else 'boss'}"
    entity_id = fact.get("entity_id")
    return f"{kind}_{entity_id}" if entity_id is not None else kind


def _position_text(value: Any, *, integer: bool) -> str:
    if not isinstance(value, Mapping):
        return "(?)"
    if integer:
        values = [str(int(float(value.get(axis, 0)))) for axis in "xyz"]
    else:
        values = [f"{float(value.get(axis, 0)):.1f}" for axis in "xyz"]
    return f"({','.join(values)})"


def _distance_text(value: Any, language: str, *, nearest: bool = False) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    prefix = "nearest distance" if nearest else "distance"
    if language == "en":
        return f"{prefix} {float(value):.1f} blocks"
    return f"{'最近距离' if nearest else '距离'} {float(value):.1f} 格"


def _direction_text(value: Any, language: str) -> str | None:
    if value is None:
        return None
    return str(value) if language == "en" else _DIRECTION_ZH.get(str(value), str(value))


def _ratio(current: Any, maximum: Any) -> str | None:
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        return None
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        return f"{fmt_number(current)}/{fmt_number(maximum)}"
    return fmt_number(current)


def _unknown_number(value: Any, language: str) -> str:
    if value is None:
        return "unknown" if language == "en" else "未知"
    return fmt_number(value)


def _health_text(
    current: Any,
    maximum: Any,
    delta: Any,
    language: str,
) -> str | None:
    current_text = _ratio(current, maximum)
    if current_text is None:
        return None
    if delta is None:
        return current_text
    try:
        change = float(delta)
    except (TypeError, ValueError):
        return current_text
    if not math.isfinite(change):
        return current_text
    if change > 0:
        label = "increased" if language == "en" else "增加"
        change_text = f"{label} {fmt_number(change)}"
    elif change < 0:
        label = "decreased" if language == "en" else "减少"
        change_text = f"{label} {fmt_number(abs(change))}"
    else:
        change_text = "unchanged" if language == "en" else "无变化"
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        return (
            f"{fmt_number(current)} ({change_text})/{fmt_number(maximum)}"
            if language == "en"
            else f"{fmt_number(current)}（{change_text}）/{fmt_number(maximum)}"
        )
    return (
        f"{fmt_number(current)} ({change_text})"
        if language == "en"
        else f"{fmt_number(current)}（{change_text}）"
    )


def _internal_status_text(status: Any, reason: Any, language: str) -> str:
    if language == "en":
        text = {
            "out_of_range": "internal state unavailable: out of use range",
            "line_of_sight_blocked": "internal state unavailable: line of sight blocked",
            "query_failed": "internal state unknown: query failed",
            "disabled": "internal enhancement disabled",
            "access_denied": "internal state unavailable: private container",
        }.get(status, "internal state unknown")
        return f"{text} ({reason})" if reason else text
    text = {
        "out_of_range": "超出使用范围；内部状态不可见",
        "line_of_sight_blocked": "视线受阻；内部状态不可见",
        "query_failed": "内部状态未知，查询失败",
        "disabled": "内部增强感知未启用",
        "access_denied": "私人容器；内部状态不可见",
    }.get(status, "内部状态未知")
    return f"{text}（{reason}）" if reason else text


def _join(parts: list[str], language: str) -> str:
    cleaned = [str(part).strip("；; ") for part in parts if str(part).strip()]
    separator = "; " if language == "en" else "；"
    ending = "." if language == "en" else "。"
    return separator.join(cleaned) + ending


__all__ = ["WORLD_SECTION_TITLES", "WorldSectionRule"]
