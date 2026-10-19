"""Task-state metadata extraction for AgenticCache."""

from __future__ import annotations

from typing import Any, Mapping

from src.memory import TeamMemory


def metadata_from_observation(
    observation: Any,
    *,
    max_fields: int,
) -> dict[str, float]:
    """Extract bounded numeric metadata from structured task perception."""
    result: dict[str, float] = {}
    if not isinstance(observation, Mapping):
        return result
    task = observation.get("task")
    if isinstance(task, Mapping):
        facts = task.get("facts")
        if isinstance(facts, list) and facts:
            _flatten_numeric("task", facts[0], result, max_fields=max_fields)
    sections = observation.get("sections")
    if isinstance(sections, Mapping):
        for section_name in sorted(sections):
            section = sections[section_name]
            if not isinstance(section, Mapping):
                continue
            summary = section.get("summary")
            if isinstance(summary, Mapping):
                _flatten_numeric(
                    f"section.{section_name}.summary",
                    summary,
                    result,
                    max_fields=max_fields,
                )
            facts = section.get("facts")
            if isinstance(facts, list):
                result.setdefault(f"section.{section_name}.fact_count", float(len(facts)))
            if len(result) >= max_fields:
                break
    return _trim(result, max_fields)


def current_state_metadata(
    *,
    memory: TeamMemory,
    agent_id: str,
    metrics: Mapping[str, Any],
    elapsed_seconds: float,
    remaining_seconds: float | None,
    action_index: int,
    max_fields: int,
) -> dict[str, float]:
    result: dict[str, float] = {
        "action_index": float(max(0, action_index)),
        "elapsed_seconds": float(max(0.0, elapsed_seconds)),
    }
    if remaining_seconds is not None:
        result["remaining_seconds"] = float(max(0.0, remaining_seconds))
    _flatten_numeric("metric", metrics, result, max_fields=max_fields)
    observation = memory.get_observation(agent_id)
    if observation is not None:
        cache_meta = observation.metadata.get("agentic_cache_metadata")
        if isinstance(cache_meta, Mapping):
            for key, value in cache_meta.items():
                if len(result) >= max_fields:
                    break
                if _is_number(value):
                    result.setdefault(str(key), float(value))
    return _trim(result, max_fields)


def _flatten_numeric(
    prefix: str,
    value: Any,
    output: dict[str, float],
    *,
    max_fields: int,
    depth: int = 0,
) -> None:
    if len(output) >= max_fields or depth > 3:
        return
    if _is_number(value):
        output.setdefault(prefix, float(value))
        return
    if isinstance(value, Mapping):
        for key in sorted(value):
            if len(output) >= max_fields:
                return
            item = value[key]
            if isinstance(item, (Mapping, list, tuple)) or _is_number(item):
                _flatten_numeric(
                    f"{prefix}.{key}",
                    item,
                    output,
                    max_fields=max_fields,
                    depth=depth + 1,
                )
        return
    if isinstance(value, (list, tuple)):
        output.setdefault(f"{prefix}.count", float(len(value)))
        for index, item in enumerate(value[:3]):
            if len(output) >= max_fields:
                return
            if isinstance(item, Mapping):
                _flatten_numeric(
                    f"{prefix}.{index}",
                    item,
                    output,
                    max_fields=max_fields,
                    depth=depth + 1,
                )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _trim(values: dict[str, float], max_fields: int) -> dict[str, float]:
    return {
        key: values[key]
        for key in sorted(values)[:max_fields]
    }


__all__ = [
    "current_state_metadata",
    "metadata_from_observation",
]
