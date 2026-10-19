"""Terminal rendering for task-level perception progress facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import VerbalizeContext, fmt_number, fmt_position


@dataclass(frozen=True)
class TaskProgressRule:
    """Render Manager/config progress without re-filtering world facts."""

    def render(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        task = observation.get("task") if isinstance(observation, dict) else None
        if not isinstance(task, dict):
            return None
        facts = task.get("facts") or []
        if not facts or not isinstance(facts[0], dict):
            return None
        fact = facts[0]
        title = "[Task Progress]" if context.language == "en" else "【任务进度】"
        lines = _render_progress(fact, context.language)
        return title + "\n" + "\n".join(lines)


def _render_progress(fact: dict[str, Any], language: str) -> list[str]:
    category = fact.get("category")
    # 清剿与 Boss 使用新格式（时间：当前/最大），不再输出旧的已用时/剩余行。
    if category in ("boss_combat", "mining", "monster_clearance", "pvp"):
        lines: list[str] = []
    else:
        lines = [_time_line(fact, language)]
    if category == "boss_combat":
        lines.extend(_boss_lines(fact, language))
    elif category == "mining":
        lines.extend(_mining_lines(fact, language))
    elif category == "monster_clearance":
        lines.extend(_monster_lines(fact, language))
    elif category == "pvp":
        lines.extend(_pvp_lines(fact, language))
    elif category == "trade":
        lines.extend(_trade_lines(fact, language))
    return lines


def _time_line(fact: dict[str, Any], language: str) -> str:
    elapsed = fmt_number(fact.get("elapsed_seconds"))
    total = fmt_number(_total_seconds(fact))
    if language == "en":
        return f"Time: {elapsed}/{total}s."
    return f"时间：{elapsed}/{total} 秒。"


def _boss_lines(fact: dict[str, Any], language: str) -> list[str]:
    current = _unknown_number(fact.get("current_health"), language)
    maximum = _unknown_number(fact.get("max_health"), language)
    alive = fact.get("alive_agents") or []
    if language == "en":
        return [
            f"Time: {_fmt_time(fact, language)}; Boss health: {current}/{maximum}; "
            f"alive: {_names(alive, language)}."
        ]
    return [
        f"时间：{_fmt_time(fact, language)}；Boss血量：{current}/{maximum}；"
        f"存活：{_names(alive, language)}。"
    ]


def _mining_lines(fact: dict[str, Any], language: str) -> list[str]:
    current = _unknown_number(fact.get("current_value"), language)
    target = _unknown_number(fact.get("target_value"), language)
    if language == "en":
        return [f"Time: {_fmt_time(fact, language)}; collection progress: {current}/{target}."]
    return [f"时间：{_fmt_time(fact, language)}；收集进度：{current}/{target}。"]


def _monster_lines(fact: dict[str, Any], language: str) -> list[str]:
    eliminated = _unknown_number(fact.get("eliminated_monsters"), language)
    alive_agents = fact.get("alive_agents") or []
    if language == "en":
        return [
            f"Time: {_fmt_time(fact, language)}; eliminated: {eliminated}; "
            f"alive: {_names(alive_agents, language)}"
        ]
    return [
        f"时间：{_fmt_time(fact, language)}；已消灭：{eliminated}；"
        f"存活：{_names(alive_agents, language)}"
    ]


def _pvp_lines(fact: dict[str, Any], language: str) -> list[str]:
    teams = _pvp_team_summary(fact.get("teams"), language)
    if language == "en":
        return [
            f"Time: {_fmt_time(fact, language)}s",
            f"Teams: {teams}",
        ]
    return [
        f"时间：{_fmt_time(fact, language)} 秒",
        f"队伍：{teams}",
    ]


def _fmt_time(fact: dict[str, Any], language: str) -> str:
    """Render ``elapsed/total`` where total = elapsed + remaining."""
    elapsed = fact.get("elapsed_seconds")
    total = _total_seconds(fact)
    return f"{_unknown_number(elapsed, language)}/{_unknown_number(total, language)}"


def _total_seconds(fact: dict[str, Any]) -> Any:
    elapsed = fact.get("elapsed_seconds")
    remaining = fact.get("remaining_seconds")
    try:
        total = max(0.0, float(elapsed) + float(remaining))
    except (TypeError, ValueError):
        total = None
    return total


def _trade_lines(fact: dict[str, Any], language: str) -> list[str]:
    targets = fact.get("targets") or {}
    target_parts: list[str] = []
    for item, detail in sorted(targets.items()):
        if not isinstance(detail, dict):
            continue
        required = detail.get("required")
        current = detail.get("current")
        missing = detail.get("missing")
        if language == "en":
            target_parts.append(
                f"{_item_name(item)} {current}/{required}, missing {missing}"
            )
        else:
            target_parts.append(
                f"{_item_name(item)} {current}/{required}，缺少 {missing}"
            )
    target_text = ("; ".join(target_parts) if language == "en" else "；".join(target_parts)) or (
        "unknown" if language == "en" else "未知"
    )
    shops = _names(fact.get("shops") or [], language)
    if language == "en":
        lines = [
            f"{fact.get('agent')}'s targets: {target_text}.",
            f"Personal box {fact.get('personal_box')}; own shop {fact.get('own_shop')}; registered shops: {shops}.",
            f"Completed agents {fact.get('completed_agents')}/{fact.get('total_agents')}.",
        ]
        if fact.get("query_error"):
            lines.append(f"Personal box query failed: {fact['query_error']}.")
        return lines
    lines = [
        f"{fact.get('agent')} 的目标：{target_text}。",
        f"私人箱 {fact.get('personal_box')}；自有商铺 {fact.get('own_shop')}；已注册商铺：{shops}。",
        f"已完成 Agent {fact.get('completed_agents')}/{fact.get('total_agents')}。",
    ]
    if fact.get("query_error"):
        lines.append(f"私人箱查询失败：{fact['query_error']}。")
    return lines


def _item_name(value: Any) -> str:
    return str(value).split(":", 1)[-1]


def _unknown_number(value: Any, language: str) -> str:
    return ("unknown" if language == "en" else "未知") if value is None else fmt_number(value)


def _percent(value: Any, language: str) -> str:
    if value is None:
        return "unknown" if language == "en" else "未知"
    try:
        return f"{fmt_number(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "unknown" if language == "en" else "未知"


def _names(values: list[Any], language: str) -> str:
    if not values:
        return "none" if language == "en" else "无"
    return ", ".join(map(str, values)) if language == "en" else "、".join(map(str, values))


def _count_map(value: Any, language: str) -> str:
    if not isinstance(value, dict) or not value:
        return "none" if language == "en" else "无"
    separator = ", " if language == "en" else "、"
    return separator.join(f"{name} x{count}" for name, count in sorted(value.items()))


def _prob_map(value: Any, language: str) -> str:
    if not isinstance(value, dict) or not value:
        return "none" if language == "en" else "无"
    separator = ", " if language == "en" else "、"
    return separator.join(
        f"{name} {fmt_number(float(probability) * 100)}%"
        for name, probability in sorted(value.items())
        if isinstance(probability, (int, float))
    )


def _pvp_team_summary(value: Any, language: str) -> str:
    if not isinstance(value, dict) or not value:
        return "unknown" if language == "en" else "未知"
    parts = []
    for team_id, data in value.items():
        if isinstance(data, dict):
            alive = data.get("alive_count")
            total = len(data.get("agents") or [])
            parts.append(f"{team_id} {alive}/{total}")
    return (", ".join(parts) if language == "en" else " 、".join(parts)) or (
        "unknown" if language == "en" else "未知"
    )


def _bounds_text(value: Any, language: str) -> str:
    if not isinstance(value, dict):
        return "unknown" if language == "en" else "未知"
    try:
        return (
            f"x={value['min_x']}..{value['max_x']}, "
            f"z={value['min_z']}..{value['max_z']}"
        )
    except KeyError:
        return "unknown" if language == "en" else "未知"


def _yes_no(value: Any, language: str) -> str:
    if value is None:
        return "unknown" if language == "en" else "未知"
    if language == "en":
        return "yes" if value else "no"
    return "是" if value else "否"


__all__ = ["TaskProgressRule"]
