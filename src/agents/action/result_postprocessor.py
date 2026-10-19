"""Post-process action results before injecting them into LLM prompts."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import importlib.util
import json
from pathlib import Path
import threading
from typing import Any


SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_BOSS_ENTITY_NAMES = frozenset({"wither", "ender_dragon"})
_POSTPROCESSOR_CACHE_LOCK = threading.RLock()
_POSTPROCESSOR_CACHE: dict[str, "_PostprocessorDirectoryCache"] = {}


class _PostprocessorDirectoryCache:
    """Cached manifest index and handlers for one static skills directory."""

    def __init__(self, base_dir: Path) -> None:
        manifests: dict[str, list[Path]] = {}
        for manifest_path in sorted(base_dir.rglob("skill.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            skill_name = manifest.get("name")
            manifests.setdefault(skill_name, []).append(manifest_path)
        self.manifests = {
            name: tuple(paths) for name, paths in manifests.items()
        }
        self.handlers: dict[str, Any] = {}


def postprocess_action_result(action_result: Any, *, skills_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a JSON-safe action result, adding ``description`` when a skill handler exists."""
    data = to_plain_dict(action_result)
    if not data:
        return data

    existing = existing_description(data)
    if existing:
        data["description"] = existing
        return data

    complete_description = describe_complete(data)
    if complete_description:
        data["description"] = complete_description
        return data

    action = str(data.get("action") or "")
    if "." not in action:
        return data

    skill_name, command = action.split(".", 1)
    handler = load_skill_postprocessor(skill_name, skills_dir=skills_dir)
    if handler is None:
        return data

    try:
        description = handler(command, data)
    except Exception:
        description = None
    if description:
        data["description"] = str(description)
    return data


def action_result_prompt_text(action_result: Any) -> str:
    """Return the compact text injected into a prompt for an action result."""
    data = to_plain_dict(action_result)
    if not data:
        return "无上一步动作结果。"
    description = existing_description(data)
    if description:
        return description
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_skill_postprocessor(skill_name: str, *, skills_dir: str | Path | None = None):
    base_dir = (Path(skills_dir) if skills_dir is not None else SKILLS_DIR).resolve()
    cache_key = str(base_dir)
    with _POSTPROCESSOR_CACHE_LOCK:
        directory_cache = _POSTPROCESSOR_CACHE.get(cache_key)
        if directory_cache is None:
            directory_cache = _PostprocessorDirectoryCache(base_dir)
            _POSTPROCESSOR_CACHE[cache_key] = directory_cache

        matching_manifests = directory_cache.manifests.get(skill_name, ())
        if len(matching_manifests) > 1:
            raise ValueError(
                f"Duplicate skill name {skill_name!r}: {list(matching_manifests)}"
            )
        if skill_name in directory_cache.handlers:
            return directory_cache.handlers[skill_name]
        if not matching_manifests:
            directory_cache.handlers[skill_name] = None
            return None

        path = matching_manifests[0].parent / "postprocess.py"
        if not path.exists():
            directory_cache.handlers[skill_name] = None
            return None

        module_name = (
            f"_mcagent_skill_{skill_name}_postprocess_{abs(hash(str(path)))}"
        )
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            directory_cache.handlers[skill_name] = None
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = getattr(module, "describe_result", None)
        if not callable(handler):
            handler = getattr(module, "describe", None)
        cached_handler = handler if callable(handler) else None
        directory_cache.handlers[skill_name] = cached_handler
        return cached_handler


def to_plain_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return json_safe(value)
    if is_dataclass(value):
        return json_safe(asdict(value))
    if hasattr(value, "to_dict"):
        return json_safe(value.to_dict())
    if hasattr(value, "__dict__"):
        return json_safe(dict(value.__dict__))
    return {"value": json_safe(value)}


def json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def existing_description(data: dict[str, Any]) -> str | None:
    direct = data.get("description")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    result = data.get("result")
    if isinstance(result, dict):
        nested = result.get("description")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def describe_complete(data: dict[str, Any]) -> str | None:
    payload = result_payload(data)
    if not payload.get("complete"):
        return None
    reason = payload.get("reason") or data.get("stop_reason") or "任务完成"
    return f"任务已完成：{reason}。"


def result_payload(action_result: dict[str, Any]) -> dict[str, Any]:
    result = action_result.get("result")
    return result if isinstance(result, dict) else {}


def result_ok(action_result: dict[str, Any], payload: dict[str, Any] | None = None) -> bool:
    payload = payload if payload is not None else result_payload(action_result)
    if isinstance(payload, dict) and "ok" in payload:
        return bool(payload["ok"])
    if "ok" in action_result:
        return bool(action_result["ok"])
    return bool(payload.get("ok"))


def error_reason(action_result: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    payload = payload if payload is not None else result_payload(action_result)
    return str(
        payload.get("error_code")
        or payload.get("reason")
        or action_result.get("error")
        or "unknown_error"
    )


def item_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item) if item is not None else "unknown"
    return str(
        item.get("name")
        or item.get("displayName")
        or item.get("item")
        or item.get("food")
        or "unknown"
    )


def item_count(item: Any, *, keys: tuple[str, ...] = ("count", "picked", "placed", "crafted_item_count")) -> int | float | None:
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and value != 0:
            return value
    return None


def format_item(item: Any, *, count_keys: tuple[str, ...] = ("count", "picked", "placed", "crafted_item_count")) -> str:
    name = item_name(item)
    count = item_count(item, keys=count_keys)
    return f"{name} x{count}" if count is not None else name


def format_items(
    items: Any,
    *,
    limit: int = 8,
    count_keys: tuple[str, ...] = ("count", "picked", "placed", "crafted_item_count"),
) -> str:
    if not isinstance(items, list) or not items:
        return "无"

    summarized = []
    for item in items[:limit]:
        summarized.append(format_item(item, count_keys=count_keys))
    if len(items) > limit:
        summarized.append(f"另有 {len(items) - limit} 项")
    return "、".join(summarized)


def format_missing(items: Any, *, limit: int = 8) -> str:
    if not isinstance(items, list) or not items:
        return "无"
    parts = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        name = item_name(item)
        missing = item.get("missing")
        required = item.get("required")
        available = item.get("available")
        if isinstance(missing, (int, float)):
            parts.append(f"{name} 缺少 x{missing}")
        elif isinstance(required, (int, float)) and isinstance(available, (int, float)):
            parts.append(f"{name} 需要 x{required}，当前 x{available}")
        else:
            parts.append(format_item(item))
    if len(items) > limit:
        parts.append(f"另有 {len(items) - limit} 项")
    return "、".join(parts)


def format_position(position: Any) -> str:
    if not isinstance(position, dict):
        return "未知位置"
    values = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, float):
            value = round(value, 2)
        values.append(f"{axis}:{value}")
    return "{" + ",".join(values) + "}"


def format_block(block: Any) -> str:
    if not isinstance(block, dict):
        return "unknown"
    name = block.get("name") or block.get("displayName") or "block"
    position = format_position(block.get("position"))
    distance = block.get("distance")
    distance_text = f"，距离 {round(distance, 2)}" if isinstance(distance, (int, float)) else ""
    return f"{name} {position}{distance_text}"


def format_entity(entity: Any) -> str:
    if not isinstance(entity, dict):
        return "unknown"
    name = entity.get("name") or entity.get("displayName") or entity.get("type") or "entity"
    normalized_names = {
        str(entity.get(key) or "").split(":", 1)[-1].strip().lower()
        for key in ("name", "displayName", "type", "kind", "mobType")
    }
    distance = entity.get("distance")
    distance_text = f"，距离 {round(distance, 2)}" if isinstance(distance, (int, float)) else ""
    if "boss" in (entity.get("roles") or []) or normalized_names & _BOSS_ENTITY_NAMES:
        name = next(
            (
                str(entity.get(key)).split(":", 1)[-1]
                for key in ("name", "displayName", "type", "kind", "mobType")
                if str(entity.get(key) or "").split(":", 1)[-1].strip().lower()
                in _BOSS_ENTITY_NAMES
            ),
            str(name),
        )
        entity_id = entity.get("id")
        if entity_id is None:
            entity_id = entity.get("entity_id")
        if isinstance(entity_id, bool) or not (
            isinstance(entity_id, int)
            or (isinstance(entity_id, str) and entity_id.strip().isdigit())
        ):
            entity_id = "boss"
        return f"{name}_{entity_id}{distance_text}"
    entity_id = entity.get("id")
    if entity_id is None:
        entity_id = entity.get("entity_id")
    id_text = f"(id={entity_id})" if entity_id is not None else ""
    return f"{name}{id_text}{distance_text}"


def format_equipment(equipment: Any) -> str:
    if not isinstance(equipment, dict):
        return "无装备信息"
    parts = []
    for slot in ("hand", "off-hand", "head", "torso", "legs", "feet"):
        item = equipment.get(slot)
        if item:
            parts.append(f"{slot}={item_name(item)}")
    return "、".join(parts) if parts else "无装备"
