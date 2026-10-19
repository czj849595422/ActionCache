"""Commander-facing perception summaries for the AutoGen baseline."""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.memory import TeamMemory


_SECTION_RE = re.compile(r"(?:【(?P<zh>[^】]+)】|\[(?P<en>[^\]]+)\])")
_COORD_RE = re.compile(
    r"\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)"
)
_DISTANCE_PREFIX_RE = re.compile(
    r"(?:最近距离|距离|nearest distance|distance)\s*[:：]?\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_DISTANCE_AWAY_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s+blocks?\s+away",
    re.IGNORECASE,
)
_ENTITY_ID_RE = re.compile(r"\b[a-zA-Z]+_\d+\b")

_SELF_TITLES = ("自身", "Self")
_EQUIPMENT_TITLES = ("手持与装备", "Held and Equipment")
_INVENTORY_TITLES = ("背包", "Inventory")
_HOSTILE_TITLES = ("敌对实体", "Hostile Entities", "Hostiles")
_DROPPED_ITEM_TITLES = ("附近掉落物", "Nearby Dropped Items", "Dropped Items")


def render_commander_perception(*, memory: TeamMemory, spec: Any) -> str:
    """Render compact global perception for the non-embodied Commander."""

    observations = memory.observations()
    if not observations:
        return "No observations yet."

    radius = _observation_range(spec)
    agent_lines: list[str] = []
    hostiles: dict[tuple[Any, ...], dict[str, Any]] = {}
    drops: dict[tuple[Any, ...], dict[str, Any]] = {}

    for agent_id in sorted(observations):
        entry = observations[agent_id]
        text = str(entry.content or "")
        sections = _split_sections(text)
        agent_lines.append(_agent_summary_line(agent_id, text, sections))
        _collect_section_facts(
            hostiles,
            _section(sections, _HOSTILE_TITLES),
            agent_id=agent_id,
            radius=radius,
            key_builder=_hostile_key,
        )
        _collect_section_facts(
            drops,
            _section(sections, _DROPPED_ITEM_TITLES),
            agent_id=agent_id,
            radius=radius,
            key_builder=_dropped_item_key,
        )

    return "\n".join(
        (
            "Agents:",
            "\n".join(agent_lines) if agent_lines else "- none",
            "Hostile targets:",
            _fact_lines(hostiles, empty="No hostile targets observed."),
            "Dropped items:",
            _fact_lines(drops, empty="No dropped items observed."),
        )
    )


def _agent_summary_line(
    agent_id: str,
    raw_text: str,
    sections: dict[str, str],
) -> str:
    self_text = _one_line(_section(sections, _SELF_TITLES))
    if not self_text and not sections:
        self_text = _one_line(raw_text)
    equipment = _one_line(_section(sections, _EQUIPMENT_TITLES)) or "unknown"
    inventory = _one_line(_section(sections, _INVENTORY_TITLES)) or "unknown"
    return (
        f"- {agent_id}: 自身: {self_text or 'unknown'} | "
        f"手持与装备: {equipment} | 背包: {inventory}"
    )


def _collect_section_facts(
    target: dict[tuple[Any, ...], dict[str, Any]],
    section_body: str,
    *,
    agent_id: str,
    radius: float | None,
    key_builder: Any,
) -> None:
    for line in _fact_candidates(section_body):
        if not _within_range(line, radius):
            continue
        key = key_builder(line)
        current = target.get(key)
        if current is None:
            target[key] = {"text": line, "seen_by": [agent_id]}
            continue
        if agent_id not in current["seen_by"]:
            current["seen_by"].append(agent_id)


def _fact_candidates(section_body: str) -> Iterable[str]:
    for line in section_body.splitlines():
        text = line.strip()
        if not text or _is_empty_fact(text):
            continue
        yield text


def _fact_lines(
    facts: dict[tuple[Any, ...], dict[str, Any]],
    *,
    empty: str,
) -> str:
    if not facts:
        return empty
    lines = []
    for value in facts.values():
        seen_by = ",".join(value["seen_by"])
        lines.append(f"- {value['text']} | seen_by={seen_by}")
    return "\n".join(lines)


def _split_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = (match.group("zh") or match.group("en") or "").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if title:
            sections[title] = body
    return sections


def _section(sections: dict[str, str], titles: Iterable[str]) -> str:
    for title in titles:
        value = sections.get(title)
        if value is not None:
            return value
    return ""


def _one_line(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _within_range(line: str, radius: float | None) -> bool:
    if radius is None:
        return True
    distance = _distance(line)
    return distance is None or distance <= radius


def _distance(line: str) -> float | None:
    match = _DISTANCE_PREFIX_RE.search(line) or _DISTANCE_AWAY_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _hostile_key(line: str) -> tuple[Any, ...]:
    match = _ENTITY_ID_RE.search(line)
    if match:
        return ("entity_id", match.group(0).lower())
    return ("entity", _leading_label(line).lower(), _position_key(line))


def _dropped_item_key(line: str) -> tuple[Any, ...]:
    return ("drop", _item_label(line).lower(), _position_key(line))


def _leading_label(line: str) -> str:
    for delimiter in ("；", ";", "，", ","):
        if delimiter in line:
            return line.split(delimiter, 1)[0].strip()
    return line.strip()


def _item_label(line: str) -> str:
    label = _leading_label(line)
    label = re.sub(r"×\d+", "", label)
    label = re.sub(r"\bx\s*\d+\b", "", label, flags=re.IGNORECASE)
    return label.strip() or label


def _position_key(line: str) -> tuple[float, float, float] | None:
    match = _COORD_RE.search(line)
    if not match:
        return None
    try:
        return tuple(round(float(value), 1) for value in match.groups())
    except (TypeError, ValueError):
        return None


def _is_empty_fact(line: str) -> bool:
    lowered = line.lower()
    return (
        "未发现" in line
        or "未展开" in line
        or "omitted" in lowered
        or ("no " in lowered and "observed" in lowered)
    )


def _observation_range(spec: Any) -> float | None:
    value = getattr(spec, "observation_range", None)
    try:
        radius = float(value)
    except (TypeError, ValueError):
        return None
    return radius if radius > 0 else None


__all__ = ["render_commander_perception"]
