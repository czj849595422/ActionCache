"""
Base types and helpers for converting filtered perception JSON to text.

The verbalizer layer is intentionally terminal: it reads structured
observation data and emits text for prompts, but it does not filter or mutate
perception data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


SUPPORTED_LANGUAGES = {"zh", "en"}


def normalize_language(language: str | None) -> str:
    """Normalize and validate verbalizer language."""
    normalized = (language or "zh").strip().lower()
    if normalized not in SUPPORTED_LANGUAGES:
        raise ValueError("language must be one of: zh, en.")
    return normalized


@dataclass
class VerbalizeContext:
    """Runtime context shared by verbalizer rules."""

    language: str = "zh"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.language = normalize_language(self.language)


class VerbalizeRule(Protocol):
    """Protocol implemented by terminal text-rendering rules."""

    def render(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        """Return a text segment or None when no segment should be emitted."""
        ...


class SectionRule:
    """Base class for titled text sections."""

    zh_title = ""
    en_title = ""

    def render(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        body = self.render_body(observation, context)
        if body is None:
            return None
        body = body.strip()
        if not body:
            return None
        title = self.title(context.language)
        if not title:
            return body
        return f"{title}{body}"

    def render_body(self, observation: dict[str, Any], context: VerbalizeContext) -> str | None:
        raise NotImplementedError

    def title(self, language: str) -> str:
        if language == "en":
            return f"[{self.en_title}] " if self.en_title else ""
        return f"【{self.zh_title}】" if self.zh_title else ""


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path from nested dict data."""
    current = data
    for part in [item for item in path.split(".") if item]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def fmt_number(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}"


def fmt_position(position: Any, language: str = "zh") -> str:
    if not isinstance(position, dict):
        return "未知" if language == "zh" else "unknown"
    return (
        f"({fmt_number(position.get('x'))}, "
        f"{fmt_number(position.get('y'))}, "
        f"{fmt_number(position.get('z'))})"
    )


def fmt_distance(value: Any, language: str = "zh") -> str:
    if value is None:
        return "未知距离" if language == "zh" else "unknown distance"
    suffix = "格" if language == "zh" else "blocks"
    return f"{fmt_number(value)} {suffix}"


def item_name(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    return (
        item.get("name")
        or item.get("displayName")
        or item.get("item", {}).get("name")
        or item.get("item", {}).get("displayName")
    )


def item_count(item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    return item.get("count") or item.get("item", {}).get("count")


def compact_item(item: Any) -> str | None:
    name = item_name(item)
    if not name:
        return None
    count = item_count(item)
    return f"{name} x{count}" if count is not None else str(name)


def first_present(data: dict[str, Any], *paths: str, default: Any = None) -> Any:
    for path in paths:
        value = get_path(data, path, None)
        if value is not None:
            return value
    return default
