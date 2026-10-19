"""Trade 配置的最小身份上下文渲染。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import threading
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from src.agents.action.result_postprocessor import result_ok, result_payload


@lru_cache(maxsize=32)
def _load_config(path: str):
    from .config import load_trade_config

    return load_trade_config(Path(path))


def _config_for_spec(spec: Any):
    params = getattr(spec, "params", None) or {}
    resolver = getattr(spec, "resolve_resource", None)
    value = params.get("trade_config_file")
    if value and callable(resolver):
        return _load_config(str(resolver(value).resolve()))
    raw_config = params.get("trade_config")
    if isinstance(raw_config, dict):
        from .config import TradeTaskConfig

        return TradeTaskConfig.from_dict(raw_config)
    return None


def _item_name(item: str) -> str:
    return item.split(":", 1)[-1]


def _result_payload(result: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = result_payload(dict(result))
    return payload if isinstance(payload, Mapping) else {}


def _result_succeeded(result: Mapping[str, Any], status: str | None) -> bool:
    if status not in {None, "completed"}:
        return False
    if result.get("ok") is False:
        return False
    payload = _result_payload(result)
    return result_ok(dict(result), dict(payload))


class TradeRecipeHistory:
    """Per-run, per-agent recipe knowledge for trade prompts."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._items: dict[str, dict[str, None]] = {}
        self._lock = threading.RLock()

    @property
    def queryable_items(self) -> frozenset[str]:
        items = set(self.config.base_price_map)
        items.update(self.config.recipe_map)
        for recipe in self.config.recipe_map.values():
            items.update(item for item, _ in recipe.ingredients)
        return frozenset(items)

    def record_action_result(
        self,
        agent_id: str,
        action: str,
        params: Mapping[str, Any] | None,
        result: Mapping[str, Any],
        status: str | None,
    ) -> bool:
        """Record only a successful query for a configured recipe output."""
        if action != "crafting.query_recipe" or not _result_succeeded(result, status):
            return False
        payload = _result_payload(result)
        raw_item = (params or {}).get("item")
        if raw_item is None:
            raw = result.get("raw")
            raw_params = raw.get("params") if isinstance(raw, Mapping) else None
            if isinstance(raw_params, Mapping):
                raw_item = raw_params.get("item")
        if raw_item is None:
            raw_item = payload.get("item")
        item = _item_id(raw_item) if raw_item is not None else ""
        if item not in self.config.recipe_map:
            return False
        if not payload.get("recipes"):
            return False
        with self._lock:
            self._items.setdefault(str(agent_id), {})[item] = None
        return True

    def sync_from_history(
        self,
        agent_id: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Recover recipe knowledge from completed action history before prompting."""
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("agent_id") or "") != str(agent_id):
                continue
            content = entry.get("content")
            if not isinstance(content, Mapping):
                continue
            result = content.get("result_data")
            if not isinstance(result, Mapping):
                continue
            action = str(result.get("action") or "")
            if action != "crafting.query_recipe":
                continue
            raw = result.get("raw")
            raw_params = raw.get("params") if isinstance(raw, Mapping) else None
            params = raw_params if isinstance(raw_params, Mapping) else None
            self.record_action_result(
                str(agent_id),
                action,
                params,
                result,
                str(content.get("result_status") or result.get("status") or ""),
            )

    def render(self, agent_id: str) -> str:
        with self._lock:
            items = tuple(self._items.get(str(agent_id), {}))
        if not items:
            return ""
        lines = ["【已查询配方】"]
        recipe_map = self.config.recipe_map
        for output in items:
            recipe = recipe_map[output]
            ingredients = "，".join(
                f"{_item_name(item)} {count}个"
                for item, count in recipe.ingredients
            )
            lines.append(f"{_item_name(output)}：{ingredients}。")
        return "\n".join(lines)


def filter_trade_recipe_history(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep recipe knowledge in the persistent section instead of short history."""
    filtered: list[Mapping[str, Any]] = []
    for entry in entries:
        content = entry.get("content") if isinstance(entry, Mapping) else None
        if not isinstance(content, Mapping):
            filtered.append(entry)
            continue
        action = content.get("selected_action") or content.get("action")
        if action in {"crafting.query_recipe", "query_recipe"}:
            continue
        filtered.append(entry)
    return filtered


def _item_id(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("item") or value.get("id")
    text = str(value or "").strip().lower()
    return text if ":" in text else f"minecraft:{text}"


def format_trade_purchase_broadcast(
    *,
    action: str,
    result: Mapping[str, Any],
    status: str | None,
) -> str | None:
    """Format one successful trade purchase as a public announcement."""
    if action != "trade.buy_from_shop" or not _result_succeeded(result, status):
        return None
    payload = _result_payload(result)
    purchased = payload.get("purchased")
    if not isinstance(purchased, Mapping):
        return None
    buyer = str(payload.get("buyer") or "").strip()
    seller = str(payload.get("seller") or "").strip()
    item = str(purchased.get("item") or "").strip()
    count = purchased.get("count")
    if not buyer or not seller or not item or not isinstance(count, int) or count < 1:
        return None
    return f"{buyer}从{seller}购买{count}个{_item_name(item)}。"


def append_trade_recipe_section(
    messages: tuple[dict[str, str], dict[str, str]],
    *,
    spec: Any,
    agent_id: str,
    history: TradeRecipeHistory | None,
    history_entries: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, str], dict[str, str]]:
    """Append only the current trade agent's recipe knowledge to user text."""
    if getattr(spec, "category", "") != "trade" or history is None:
        return messages
    history.sync_from_history(agent_id, history_entries)
    section = history.render(agent_id)
    if not section:
        return messages
    system, user = messages
    updated_user = dict(user)
    updated_user["content"] = f"{user.get('content', '').rstrip()}\n\n{section}"
    return system, updated_user


def create_trade_recipe_history(spec: Any) -> TradeRecipeHistory | None:
    if getattr(spec, "category", "") != "trade":
        return None
    config = _config_for_spec(spec)
    return TradeRecipeHistory(config) if config is not None else None


def publish_trade_purchase_broadcast(
    *,
    memory: Any,
    entry_id: str | None,
    agent_id: str,
    action: str,
    result: Mapping[str, Any],
    status: str | None,
    broadcast: Any = None,
    broadcast_enabled: bool = True,
    event_sink: Any = None,
) -> str | None:
    text = format_trade_purchase_broadcast(
        action=action,
        result=result,
        status=status,
    )
    if not text:
        return None
    try:
        if entry_id:
            memory.update_history_event(
                entry_id,
                content={
                    "communication": text,
                    "public_broadcast": text,
                },
            )
    except Exception as exc:  # noqa: BLE001
        if callable(event_sink):
            event_sink(
                "trade.broadcast_memory_failed",
                agent_id=agent_id,
                error=str(exc),
            )
    if broadcast_enabled and callable(broadcast):
        try:
            broadcast(text)
        except Exception as exc:  # noqa: BLE001
            if callable(event_sink):
                event_sink(
                    "trade.broadcast_failed",
                    agent_id=agent_id,
                    error=str(exc),
                )
    return text


def render_trade_prompt_context(spec: Any, agent_id: str | None = None) -> str:
    """只渲染职业、姓名和目标，不暴露库存或交易求解信息。"""
    config = _config_for_spec(spec)
    if config is None:
        return ""
    labels = {
        profession_id: profession.label
        for profession_id, profession in config.professions
    }
    if agent_id is None:
        lines = ["【Agent 职业】"]
        lines.extend(
            f"- {agent.name}：{labels.get(agent.profession, agent.profession)}"
            for agent in config.agents
        )
        return "\n".join(lines)

    current = next(
        (agent for agent in config.agents if agent.name == agent_id),
        None,
    )
    if current is None:
        return ""
    lines = [
        "【当前 Agent】",
        f"名称：{current.name}",
        f"职业：{labels.get(current.profession, current.profession)}",
        "目标清单：",
    ]
    lines.extend(f"- {_item_name(target.item)}" for target in current.targets)
    lines.extend(["", "【其他 Agent 职业】"])
    lines.extend(
        f"- {agent.name}：{labels.get(agent.profession, agent.profession)}"
        for agent in config.agents
        if agent.name != current.name
    )
    return "\n".join(lines)


def render_trade_team_professions(spec: Any) -> str:
    """为 AutoGen Commander 仅提供全体 Agent 的姓名和职业。"""
    config = _config_for_spec(spec)
    if config is None:
        return ""
    labels = {
        profession_id: profession.label
        for profession_id, profession in config.professions
    }
    return "\n".join(
        f"- {agent.name}：{labels.get(agent.profession, agent.profession)}"
        for agent in config.agents
    )


__all__ = [
    "TradeRecipeHistory",
    "append_trade_recipe_section",
    "create_trade_recipe_history",
    "filter_trade_recipe_history",
    "format_trade_purchase_broadcast",
    "publish_trade_purchase_broadcast",
    "render_trade_prompt_context",
    "render_trade_team_professions",
]
