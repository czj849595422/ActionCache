"""
Reusable terminal text rules for common perception sections.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import (
    SectionRule,
    VerbalizeContext,
    compact_item,
    first_present,
    fmt_distance,
    fmt_position,
    get_path,
)


@dataclass
class SelfStatusRule(SectionRule):
    """Describe the agent's own status."""

    include_position: bool = True
    zh_title: str = "自身状态"
    en_title: str = "Self Status"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        self_state = observation.get("self") or observation.get("self_state")
        if not isinstance(self_state, dict):
            return None
        language = context.language
        health = self_state.get("health")
        food = self_state.get("food")
        game_mode = self_state.get("gameMode")
        on_ground = self_state.get("onGround")
        position = fmt_position(self_state.get("position"), language)

        if language == "en":
            parts = []
            if health is not None:
                parts.append(_health_text(self_state, language))
            if food is not None:
                parts.append(f"food {food}")
            if self.include_position:
                parts.append(f"position {position}")
            if self_state.get("recentAttackCount") is not None:
                parts.append(f"recent attacks {self_state['recentAttackCount']}")
            if game_mode is not None:
                parts.append(f"game mode {game_mode}")
            if on_ground is not None:
                parts.append(f"on ground {on_ground}")
            return "Agent " + ", ".join(parts) + "." if parts else None

        parts = []
        if health is not None:
            parts.append(_health_text(self_state, language))
        if food is not None:
            parts.append(f"饱食度 {food}")
        if self.include_position:
            parts.append(f"位置 {position}")
        if self_state.get("recentAttackCount") is not None:
            parts.append(f"近期受到攻击次数：{self_state['recentAttackCount']}次")
        if game_mode is not None:
            parts.append(f"游戏模式 {game_mode}")
        if on_ground is not None:
            parts.append(f"是否在地面 {on_ground}")
        return "你当前" + "，".join(parts) + "。" if parts else None


def _health_text(self_state: dict[str, Any], language: str) -> str:
    health = self_state.get("health")
    delta = self_state.get("healthDelta")
    if delta is None:
        return f"health {health}" if language == "en" else f"生命值 {health}"
    try:
        change = float(delta)
    except (TypeError, ValueError):
        return f"health {health}" if language == "en" else f"生命值 {health}"
    if change > 0:
        change_text = f"increased {change:g}" if language == "en" else f"增加 {change:g}"
    elif change < 0:
        change_text = f"decreased {abs(change):g}" if language == "en" else f"减少 {abs(change):g}"
    else:
        change_text = "unchanged" if language == "en" else "无变化"
    return (
        f"health {health} ({change_text})"
        if language == "en"
        else f"生命值 {health}（{change_text}）"
    )


@dataclass
class TeamStatusRule(SectionRule):
    """Describe non-self players."""

    max_players: int = 5
    zh_title: str = "队友信息"
    en_title: str = "Teammates"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        players = observation.get("players")
        if not isinstance(players, list):
            return None
        teammates = [player for player in players if isinstance(player, dict) and not player.get("isSelf")]
        if not teammates:
            return None
        language = context.language
        entries = []
        for player in teammates[: self.max_players]:
            username = player.get("username") or "unknown"
            entity = player.get("entity") if isinstance(player.get("entity"), dict) else {}
            distance = entity.get("distance")
            position = fmt_position(entity.get("position"), language)
            if language == "en":
                entries.append(f"{username} at {position}, {fmt_distance(distance, language)} away")
            else:
                entries.append(f"{username} 位于 {position}，距离 {fmt_distance(distance, language)}")
        if language == "en":
            return "; ".join(entries) + "."
        return "；".join(entries) + "。"


@dataclass
class HostileEntitiesRule(SectionRule):
    """Describe hostile entities from the filtered entity list."""

    max_entities: int = 5
    include_position: bool = True
    include_empty: bool = False
    zh_title: str = "敌人信息"
    en_title: str = "Hostiles"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        entities = observation.get("entities")
        if not isinstance(entities, list):
            return None
        hostiles = [
            entity for entity in entities
            if isinstance(entity, dict) and self._is_hostile(entity)
        ]
        language = context.language
        if not hostiles:
            if self.include_empty:
                return "No hostile entities are visible." if language == "en" else "未发现敌对实体。"
            return None

        entries = []
        for entity in hostiles[: self.max_entities]:
            name = entity.get("name") or entity.get("displayName") or entity.get("mobType") or "unknown"
            entity_id = entity.get("id") or entity.get("entityId") or entity.get("uuid")
            distance = fmt_distance(entity.get("distance"), language)
            position = fmt_position(entity.get("position"), language)
            if language == "en":
                detail = f"{name}"
                if entity_id is not None:
                    detail += f"(id={entity_id})"
                detail += f", {distance} away"
                if self.include_position:
                    detail += f", position {position}"
                entries.append(detail)
            else:
                detail = f"{name}"
                if entity_id is not None:
                    detail += f"(id={entity_id})"
                detail += f"，距离 {distance}"
                if self.include_position:
                    detail += f"，坐标 {position}"
                entries.append(detail)
        if language == "en":
            return f"{len(hostiles)} hostile entities nearby: " + "; ".join(entries) + "."
        return f"附近有 {len(hostiles)} 个敌对实体：" + "；".join(entries) + "。"

    @staticmethod
    def _is_hostile(entity: dict[str, Any]) -> bool:
        if entity.get("type") == "hostile":
            return True
        text = " ".join(
            str(entity.get(key) or "")
            for key in ("name", "displayName", "mobType", "kind")
        ).lower()
        return any(token in text for token in ("zombie", "skeleton", "spider", "creeper", "hostile"))


@dataclass
class HeldItemRule(SectionRule):
    """Describe the currently held item."""

    include_empty: bool = True
    zh_title: str = "手持物品"
    en_title: str = "Held Item"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        held_item = first_present(observation, "self.heldItem", "self.held_item")
        text = compact_item(held_item)
        if text is None:
            if not self.include_empty:
                return None
            return "No held item." if context.language == "en" else "当前没有手持物品。"
        if context.language == "en":
            return f"Currently holding {text}."
        return f"当前手持 {text}。"


@dataclass
class EquipmentRule(SectionRule):
    """Describe equipped items."""

    zh_title: str = "装备信息"
    en_title: str = "Equipment"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        equipment = first_present(observation, "self.equipment", "equipment", default=[])
        if not isinstance(equipment, list):
            return None
        items = [compact_item(item) for item in equipment]
        items = [item for item in items if item]
        if not items:
            return None
        if context.language == "en":
            return "Equipped items: " + ", ".join(items) + "."
        return "当前装备：" + "，".join(items) + "。"


@dataclass
class InventoryRule(SectionRule):
    """Describe inventory items from self state."""

    max_items: int = 8
    zh_title: str = "背包信息"
    en_title: str = "Inventory"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        inventory = get_path(observation, "self.inventory", [])
        if not isinstance(inventory, list):
            return None
        items = [compact_item(item) for item in inventory[: self.max_items]]
        items = [item for item in items if item]
        if not items:
            return None
        if context.language == "en":
            return "Inventory includes " + ", ".join(items) + "."
        return "背包中包含：" + "，".join(items) + "。"


@dataclass
class NearbyItemsRule(SectionRule):
    """Describe nearby dropped items."""

    max_items: int = 5
    zh_title: str = "附近掉落物"
    en_title: str = "Nearby Items"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        items = observation.get("items")
        if not isinstance(items, list) or not items:
            return None
        language = context.language
        entries = []
        for item in items[: self.max_items]:
            text = compact_item(item)
            if not text:
                continue
            distance = fmt_distance(item.get("distance"), language) if isinstance(item, dict) else ""
            position = fmt_position(item.get("position"), language) if isinstance(item, dict) else ""
            if language == "en":
                entries.append(f"{text}, {distance} away, position {position}")
            else:
                entries.append(f"{text}，距离 {distance}，坐标 {position}")
        if not entries:
            return None
        if language == "en":
            return "Nearby dropped items: " + "; ".join(entries) + "."
        return "附近掉落物：" + "；".join(entries) + "。"


@dataclass
class NearbyBlocksRule(SectionRule):
    """Describe nearby blocks or obstacle candidates."""

    max_blocks: int = 8
    zh_title: str = "附近方块"
    en_title: str = "Nearby Blocks"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        blocks = observation.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return None
        language = context.language
        entries = []
        for block in blocks[: self.max_blocks]:
            if not isinstance(block, dict):
                continue
            name = block.get("name") or "unknown"
            distance = fmt_distance(block.get("distance"), language)
            material = block.get("material")
            if language == "en":
                text = f"{name}, {distance} away"
                if material:
                    text += f", material {material}"
            else:
                text = f"{name}，距离 {distance}"
                if material:
                    text += f"，材料 {material}"
            entries.append(text)
        if not entries:
            return None
        if language == "en":
            return "Nearby blocks: " + "; ".join(entries) + "."
        return "附近方块：" + "；".join(entries) + "。"


@dataclass
class NearbyChestsRule(SectionRule):
    """Describe nearby chest-like containers from block observations."""

    max_chests: int = 8
    include_empty: bool = True
    zh_title: str = "附近箱子"
    en_title: str = "Nearby Chests"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        blocks = observation.get("blocks")
        if not isinstance(blocks, list):
            return None
        chests = [
            block for block in blocks
            if isinstance(block, dict) and self._is_chest(block)
        ]
        language = context.language
        if not chests:
            if self.include_empty:
                return "No nearby chests are visible." if language == "en" else "未发现附近箱子。"
            return None

        entries = []
        for chest in chests[: self.max_chests]:
            name = chest.get("name") or "chest"
            distance = fmt_distance(chest.get("distance"), language)
            position = fmt_position(chest.get("position"), language)
            if language == "en":
                entries.append(f"{name}, {distance} away, position {position}")
            else:
                entries.append(f"{name}，距离 {distance}，坐标 {position}")
        if language == "en":
            return f"{len(chests)} nearby chests: " + "; ".join(entries) + "."
        return f"附近有 {len(chests)} 个箱子：" + "；".join(entries) + "。"

    @staticmethod
    def _is_chest(block: dict[str, Any]) -> bool:
        name = str(block.get("name") or "").lower()
        return "chest" in name or "barrel" in name


@dataclass
class SummaryRule(SectionRule):
    """Describe a summary dict produced by complex perception rules."""

    zh_title: str = "摘要"
    en_title: str = "Summary"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        summary = observation.get("summary")
        if not isinstance(summary, dict) or not summary:
            return None
        language = context.language
        parts = []
        for key, value in summary.items():
            if isinstance(value, dict):
                text = ", ".join(f"{inner_key}: {inner_value}" for inner_key, inner_value in value.items())
            else:
                text = str(value)
            if language == "en":
                parts.append(f"{key}: {text}")
            else:
                parts.append(f"{key}：{text}")
        if language == "en":
            return "; ".join(parts) + "."
        return "；".join(parts) + "。"
