"""
Common verbalizer presets for filtered perception outputs.
"""

from __future__ import annotations

from typing import Mapping

from .pipeline import VerbalizerPipeline
from .rules.task import TaskProgressRule
from .rules.world import WORLD_SECTION_TITLES, WorldSectionRule
from .rules import (
    EquipmentRule,
    HeldItemRule,
    HostileEntitiesRule,
    InventoryRule,
    NearbyBlocksRule,
    NearbyChestsRule,
    NearbyItemsRule,
    SelfStatusRule,
    SummaryRule,
    TeamStatusRule,
)


def combat_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Self status + held item + equipment + hostiles."""
    return VerbalizerPipeline(
        [
            SelfStatusRule(),
            HeldItemRule(),
            EquipmentRule(),
            HostileEntitiesRule(include_empty=True),
            SummaryRule(),
        ],
        language=language,
    )


def team_combat_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Self status + teammates + hostiles."""
    return VerbalizerPipeline(
        [
            SelfStatusRule(),
            TeamStatusRule(),
            HostileEntitiesRule(include_empty=True),
            SummaryRule(),
        ],
        language=language,
    )


def task1_combat_chest_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Self status + hostiles + nearby chests for task-1 action selection."""
    return VerbalizerPipeline(
        [
            SelfStatusRule(),
            HostileEntitiesRule(include_empty=True),
            NearbyChestsRule(include_empty=True),
            SummaryRule(),
        ],
        language=language,
    )


def inventory_equipment_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Held item + equipment + inventory."""
    return VerbalizerPipeline(
        [
            HeldItemRule(),
            EquipmentRule(),
            InventoryRule(),
        ],
        language=language,
    )


def pickup_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Self status + inventory + nearby dropped items."""
    return VerbalizerPipeline(
        [
            SelfStatusRule(),
            InventoryRule(),
            NearbyItemsRule(),
            SummaryRule(),
        ],
        language=language,
    )


def navigation_verbalizer(language: str = "zh") -> VerbalizerPipeline:
    """Self status + nearby block or obstacle information."""
    return VerbalizerPipeline(
        [
            SelfStatusRule(),
            NearbyBlocksRule(),
            SummaryRule(),
        ],
        language=language,
    )


def world_verbalizer(
    language: str = "zh",
    *,
    sections: list[str] | tuple[str, ...] | None = None,
    limits: Mapping[str, int] | None = None,
    show_disabled: bool = False,
) -> VerbalizerPipeline:
    """Build the terminal renderer for selected W01-W16 world sections."""
    selected = tuple(sections) if sections is not None else tuple(WORLD_SECTION_TITLES)
    unknown = sorted(set(selected) - set(WORLD_SECTION_TITLES))
    if unknown:
        raise ValueError(f"unknown world verbalizer sections: {unknown}")
    active_limits = dict(limits or {})
    unknown_limits = sorted(set(active_limits) - set(WORLD_SECTION_TITLES))
    if unknown_limits:
        raise ValueError(f"unknown world verbalizer limits: {unknown_limits}")
    return VerbalizerPipeline(
        [
            WorldSectionRule(
                section,
                limit=active_limits.get(section),
                show_disabled=show_disabled,
            )
            for section in selected
        ],
        language=language,
    )


def task_verbalizer(
    category: str,
    language: str = "zh",
) -> VerbalizerPipeline:
    """Render one registered task profile and its Manager/config progress."""
    from src.perception.rule.task import profile_for_category

    profile = profile_for_category(category)
    return VerbalizerPipeline(
        [
            *(WorldSectionRule(section) for section in profile.sections),
            TaskProgressRule(),
        ],
        language=language,
    )
