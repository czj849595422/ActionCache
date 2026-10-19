"""Thread-safe, fail-closed filesystem recorders."""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from uuid import uuid4

import yaml

from .config import RecordingConfig
from .schemas import EpisodeContext, LLMCallSource, RecordingError, RunContext


_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUN_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S-%f"
_TOKEN_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "total_tokens": ("total_tokens",),
}


def _validate_slug(value: str, label: str) -> str:
    text = str(value).strip()
    if not text or not _SLUG_PATTERN.fullmatch(text):
        raise RecordingError(f"invalid {label}: {value!r}")
    if label == "baseline":
        return "PVP" if text == "PVP" else text.lower()
    return text


def _safe_part(value: str, *, default: str, limit: int = 48) -> str:
    """Sanitize a free-text label into a filename-safe slug segment."""
    text = re.sub(r"[^A-Za-z0-9._-]", "_", str(value).strip()).strip("._-")
    return text[:limit] or default


def _ensure_within(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise RecordingError(f"recording path escapes root: {target}") from exc
    return resolved_target


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    return str(value)


def _redact(value: Any, config: RecordingConfig) -> Any:
    sensitive = tuple(key.lower() for key in config.sensitive_keys)
    secrets = tuple(secret for secret in config.redact_values if secret)

    def visit(item: Any, key: str | None = None) -> Any:
        lowered = key.lower() if key is not None else ""
        if key is not None and any(
            lowered == part or lowered.endswith(f"_{part}")
            for part in sensitive
        ):
            return "***"
        if isinstance(item, Mapping):
            return {str(k): visit(v, str(k)) for k, v in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [visit(entry) for entry in item]
        if isinstance(item, str):
            text = item
            for secret in secrets:
                text = text.replace(secret, "***")
            text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer ***", text)
            return text
        return _json_value(item)

    return visit(_json_value(value))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _finite_number(value: Any, *, include_bool: bool = False) -> float | None:
    if isinstance(value, bool):
        return float(value) if include_bool else None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stddev(values: Sequence[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = _finite_number(started_at)
    finish = _finite_number(finished_at)
    if start is None or finish is None:
        return None
    return max(0.0, finish - start)


def _top_level_numeric_metrics(value: Mapping[str, Any] | None) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        number = _finite_number(item, include_bool=True)
        if number is not None:
            result[str(key)] = number
    return result


def _average_numeric_dicts(values: Sequence[Mapping[str, float]]) -> dict[str, float | None]:
    keys = sorted({key for value in values for key in value})
    result: dict[str, float | None] = {}
    for key in keys:
        numbers = [
            float(value[key])
            for value in values
            if (
                key in value
                and isinstance(value[key], (int, float))
                and not isinstance(value[key], bool)
            )
        ]
        result[key] = _mean(numbers)
    return result


def _stddev_numeric_dicts(values: Sequence[Mapping[str, float]]) -> dict[str, float | None]:
    keys = sorted({key for value in values for key in value})
    result: dict[str, float | None] = {}
    for key in keys:
        numbers = [
            float(value[key])
            for value in values
            if (
                key in value
                and isinstance(value[key], (int, float))
                and not isinstance(value[key], bool)
            )
        ]
        result[key] = _stddev(numbers)
    return result


def _average_nested_numeric_dicts(
    values: Sequence[Mapping[str, Mapping[str, Any]]],
) -> dict[str, dict[str, float | None]]:
    keys = sorted({key for value in values for key in value})
    result: dict[str, dict[str, float | None]] = {}
    for key in keys:
        nested_values: list[Mapping[str, Any]] = []
        for value in values:
            item = value.get(key)
            if isinstance(item, Mapping):
                nested_values.append(item)
        result[key] = _average_numeric_dicts(nested_values)
    return result


def _stddev_nested_numeric_dicts(
    values: Sequence[Mapping[str, Mapping[str, Any]]],
) -> dict[str, dict[str, float | None]]:
    keys = sorted({key for value in values for key in value})
    result: dict[str, dict[str, float | None]] = {}
    for key in keys:
        nested_values: list[Mapping[str, Any]] = []
        for value in values:
            item = value.get(key)
            if isinstance(item, Mapping):
                nested_values.append(item)
        result[key] = _stddev_numeric_dicts(nested_values)
    return result


def _llm_totals(summary: Mapping[str, Any] | None) -> dict[str, float]:
    if not isinstance(summary, Mapping):
        return {}
    tokens = summary.get("token_sums")
    result: dict[str, float] = {}
    total_calls = _finite_number(summary.get("total_calls"))
    if total_calls is not None:
        result["llm_calls"] = total_calls
    if isinstance(tokens, Mapping):
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            number = _finite_number(tokens.get(source))
            if number is not None:
                result[target] = number
    return result


def _llm_token_totals(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], dict[str, int], int]:
    token_sums = {name: 0 for name in _TOKEN_ALIASES}
    token_coverage = {name: 0 for name in _TOKEN_ALIASES}
    calls_with_usage = 0
    for item in records:
        usage = item.get("usage") or {}
        if usage.get("usage_available"):
            calls_with_usage += 1
        normalized = usage.get("normalized") or {}
        for name in token_sums:
            value = normalized.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                token_sums[name] += value
                token_coverage[name] += 1
    return token_sums, token_coverage, calls_with_usage


def _call_source_component(record: Mapping[str, Any]) -> str:
    source = record.get("source")
    if isinstance(source, Mapping):
        component = source.get("component")
        if isinstance(component, str) and component.strip():
            return component.strip()
        if component is not None:
            text = str(component).strip()
            if text:
                return text
    return "standalone"


def _llm_component_token_stats(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | None]]:
    if not records:
        return {}
    token_sums, _, _ = _llm_token_totals(records)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in records:
        grouped.setdefault(_call_source_component(item), []).append(item)
    total_calls = float(len(records))
    total_input_tokens = float(token_sums["input_tokens"])
    total_output_tokens = float(token_sums["output_tokens"])
    total_total_tokens = float(token_sums["total_tokens"])
    stats: dict[str, dict[str, float | None]] = {}
    for component in sorted(grouped):
        component_records = grouped[component]
        component_token_sums, _, _ = _llm_token_totals(component_records)
        stats[component] = {
            "call_count": float(len(component_records)),
            "call_share": _safe_ratio(float(len(component_records)), total_calls),
            "input_tokens": float(component_token_sums["input_tokens"]),
            "input_token_share": _safe_ratio(
                float(component_token_sums["input_tokens"]),
                total_input_tokens,
            ),
            "output_tokens": float(component_token_sums["output_tokens"]),
            "output_token_share": _safe_ratio(
                float(component_token_sums["output_tokens"]),
                total_output_tokens,
            ),
            "total_tokens": float(component_token_sums["total_tokens"]),
            "total_token_share": _safe_ratio(
                float(component_token_sums["total_tokens"]),
                total_total_tokens,
            ),
        }
    return stats


def _action_duration_metrics(path: Path) -> dict[str, list[float]]:
    result = {
        "planning_duration_seconds": [],
        "planning_to_execution_start_seconds": [],
        "execution_duration_seconds": [],
    }
    if not path.is_file():
        return result
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for key in ("planning_duration_seconds", "execution_duration_seconds"):
                    try:
                        number = _finite_number(float(row.get(key) or ""))
                    except ValueError:
                        number = None
                    if number is not None:
                        result[key].append(number)
                planning_started_at = _csv_float(row.get("planning_started_at"))
                execution_started_at = _csv_float(row.get("execution_started_at"))
                if planning_started_at is not None and execution_started_at is not None:
                    result["planning_to_execution_start_seconds"].append(
                        max(0.0, execution_started_at - planning_started_at)
                    )
    except Exception:  # noqa: BLE001
        return {
            "planning_duration_seconds": [],
            "planning_to_execution_start_seconds": [],
            "execution_duration_seconds": [],
        }
    return result


def _merge_action_duration_metrics(
    metrics: Sequence[Mapping[str, Sequence[float]]],
) -> dict[str, list[float]]:
    result = {
        "planning_duration_seconds": [],
        "planning_to_execution_start_seconds": [],
        "execution_duration_seconds": [],
    }
    for item in metrics:
        for key in result:
            values = item.get(key) if isinstance(item, Mapping) else None
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                number = _finite_number(value)
                if number is not None:
                    result[key].append(number)
    return result


def _pvp_team_episode_paths(
    parent_path: Path | None,
    parent_episode_id: Any,
) -> list[Path]:
    if parent_path is None or not parent_path.is_dir():
        return []
    parent_id = str(parent_episode_id or "")
    result: list[Path] = []
    try:
        children = sorted(parent_path.iterdir(), key=lambda path: path.name)
    except Exception:  # noqa: BLE001
        return []
    for child in children:
        if not child.is_dir():
            continue
        manifest = _read_json_mapping(child / "episode.json")
        if not manifest or manifest.get("kind") != "episode":
            continue
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        if str(metadata.get("parent_episode_id") or "") == parent_id:
            result.append(child)
    return result


def _sum_llm_summaries(summaries: Sequence[Mapping[str, Any] | None]) -> dict[str, float]:
    result: dict[str, float] = {}
    for summary in summaries:
        for key, value in _llm_totals(summary).items():
            result[key] = result.get(key, 0.0) + value
    return result


def _sum_component_token_stats(
    summaries: Sequence[Mapping[str, Any] | None],
) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, dict[str, float]] = {}
    for summary in summaries:
        if not isinstance(summary, Mapping):
            continue
        stats = summary.get("component_token_stats")
        if not isinstance(stats, Mapping):
            continue
        for component, item in stats.items():
            if not isinstance(item, Mapping):
                continue
            target = grouped.setdefault(
                str(component),
                {
                    "call_count": 0.0,
                    "input_tokens": 0.0,
                    "output_tokens": 0.0,
                    "total_tokens": 0.0,
                },
            )
            for key in ("call_count", "input_tokens", "output_tokens", "total_tokens"):
                number = _finite_number(item.get(key))
                if number is not None:
                    target[key] += number

    total_calls = sum(item["call_count"] for item in grouped.values())
    total_input_tokens = sum(item["input_tokens"] for item in grouped.values())
    total_output_tokens = sum(item["output_tokens"] for item in grouped.values())
    total_total_tokens = sum(item["total_tokens"] for item in grouped.values())
    result: dict[str, dict[str, float | None]] = {}
    for component in sorted(grouped):
        item = grouped[component]
        result[component] = {
            "call_count": item["call_count"],
            "call_share": _safe_ratio(item["call_count"], total_calls),
            "input_tokens": item["input_tokens"],
            "input_token_share": _safe_ratio(
                item["input_tokens"],
                total_input_tokens,
            ),
            "output_tokens": item["output_tokens"],
            "output_token_share": _safe_ratio(
                item["output_tokens"],
                total_output_tokens,
            ),
            "total_tokens": item["total_tokens"],
            "total_token_share": _safe_ratio(
                item["total_tokens"],
                total_total_tokens,
            ),
        }
    return result


def _pvp_team_result_payloads(result: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(result, Mapping):
        return []
    team_results = result.get("team_results")
    if not isinstance(team_results, list):
        return []
    payloads: list[Mapping[str, Any]] = []
    for item in team_results:
        if not isinstance(item, Mapping):
            continue
        nested = item.get("result")
        if isinstance(nested, Mapping):
            payloads.append(nested)
    return payloads


def _sum_pvp_team_result_numbers(
    result: Mapping[str, Any] | None,
    key: str,
) -> float | None:
    total = 0.0
    found = False
    for payload in _pvp_team_result_payloads(result):
        number = _finite_number(payload.get(key))
        if number is None:
            continue
        total += number
        found = True
    return total if found else None


def _pvp_team_runner_error_count(result: Mapping[str, Any] | None) -> int:
    count = 0
    if isinstance(result, Mapping) and result.get("runner_error"):
        count += 1
    if not isinstance(result, Mapping):
        return count
    team_results = result.get("team_results")
    if not isinstance(team_results, list):
        return count
    for item in team_results:
        if not isinstance(item, Mapping):
            continue
        if item.get("error"):
            count += 1
        nested = item.get("result")
        if isinstance(nested, Mapping) and nested.get("runner_error"):
            count += 1
    return count


def _pvp_team_list_error_count(
    result: Mapping[str, Any] | None,
    key: str,
) -> int:
    total = 0
    if isinstance(result, Mapping) and isinstance(result.get(key) or [], list):
        total += len(result.get(key) or [])
    for payload in _pvp_team_result_payloads(result):
        if isinstance(payload.get(key) or [], list):
            total += len(payload.get(key) or [])
    return total


def _csv_float(value: Any) -> float | None:
    try:
        return _finite_number(float(value or ""))
    except ValueError:
        return None


def _episode_aggregate_record(episode: Any) -> dict[str, Any]:
    path_value = getattr(episode, "path", None)
    path = Path(path_value) if path_value is not None else None
    episode_id = getattr(getattr(episode, "context", None), "episode_id", None)
    result_payload = _read_json_mapping(path / "result.json") if path else None
    result = result_payload.get("result") if isinstance(result_payload, Mapping) else None
    if not isinstance(result, Mapping):
        result = None
    llm_summary = _read_json_mapping(path / "llm" / "summary.json") if path else None
    pvp_team_payloads = _pvp_team_result_payloads(result)
    pvp_child_paths = (
        _pvp_team_episode_paths(path, episode_id) if pvp_team_payloads else []
    )
    llm_summaries = [llm_summary]
    llm_summaries.extend(
        _read_json_mapping(child / "llm" / "summary.json")
        for child in pvp_child_paths
    )
    if pvp_child_paths:
        component_token_stats = _sum_component_token_stats(llm_summaries)
        action_durations = _merge_action_duration_metrics(
            [
                _action_duration_metrics(child / "action_executions.csv")
                for child in [path, *pvp_child_paths]
                if child is not None
            ]
        )
        llm_totals = _sum_llm_summaries(llm_summaries)
    else:
        component_token_stats = (
            llm_summary.get("component_token_stats")
            if isinstance(llm_summary, Mapping)
            and isinstance(llm_summary.get("component_token_stats"), Mapping)
            else {}
        )
        action_durations = (
            _action_duration_metrics(path / "action_executions.csv") if path else {
                "planning_duration_seconds": [],
                "planning_to_execution_start_seconds": [],
                "execution_duration_seconds": [],
            }
        )
        llm_totals = _llm_totals(llm_summary)
    outcome = result.get("outcome") if isinstance(result, Mapping) else None
    status = None
    if isinstance(outcome, Mapping) and outcome.get("status"):
        status = str(outcome["status"])
    final_metrics = (
        result.get("final_metrics")
        if isinstance(result, Mapping) and isinstance(result.get("final_metrics"), Mapping)
        else {}
    )
    cache_stats = (
        result.get("cache_stats")
        if isinstance(result, Mapping) and isinstance(result.get("cache_stats"), Mapping)
        else {}
    )
    planned_count = (
        _finite_number(result.get("planned_count"))
        if isinstance(result, Mapping)
        else None
    )
    if planned_count is None and pvp_team_payloads:
        planned_count = _sum_pvp_team_result_numbers(result, "planned_count")
    executed_count = (
        _finite_number(result.get("executed_count"))
        if isinstance(result, Mapping)
        else None
    )
    if executed_count is None and pvp_team_payloads:
        executed_count = _sum_pvp_team_result_numbers(result, "executed_count")
    runner_error_count = (
        _pvp_team_runner_error_count(result)
        if pvp_team_payloads
        else 1 if isinstance(result, Mapping) and result.get("runner_error") else 0
    )
    worker_error_count = (
        _pvp_team_list_error_count(result, "worker_errors")
        if pvp_team_payloads
        else len(result.get("worker_errors") or [])
        if isinstance(result, Mapping) and isinstance(result.get("worker_errors") or [], list)
        else 0
    )
    cleanup_error_count = (
        _pvp_team_list_error_count(result, "cleanup_errors")
        if pvp_team_payloads
        else len(result.get("cleanup_errors") or [])
        if isinstance(result, Mapping) and isinstance(result.get("cleanup_errors") or [], list)
        else 0
    )
    return {
        "episode_id": episode_id,
        "episode_status": getattr(episode, "status", None),
        "path": path,
        "result": result,
        "result_valid": isinstance(result, Mapping),
        "outcome_status": status,
        "success": status == "success",
        "interrupted": bool(result.get("interrupted")) if isinstance(result, Mapping) else False,
        "wall_seconds": (
            _duration_seconds(result.get("started_at"), result.get("finished_at"))
            if isinstance(result, Mapping)
            else None
        ),
        "task_elapsed_seconds": (
            _finite_number(final_metrics.get("elapsed_seconds"))
            if isinstance(final_metrics, Mapping)
            else None
        ),
        "planned_count": planned_count,
        "executed_count": executed_count,
        "task_metrics": _top_level_numeric_metrics(final_metrics),
        "llm_totals": llm_totals,
        "component_token_stats": component_token_stats,
        "action_durations": action_durations,
        "runner_error_count": runner_error_count,
        "worker_error_count": worker_error_count,
        "cleanup_error_count": cleanup_error_count,
        "cache_stats": _top_level_numeric_metrics(cache_stats),
    }


def _record_values(records: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for record in records:
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


def _sum_record_values(records: Sequence[Mapping[str, Any]], key: str) -> float:
    return sum(_record_values(records, key))


def _sum_llm(records: Sequence[Mapping[str, Any]], key: str) -> float:
    total = 0.0
    for record in records:
        totals = record.get("llm_totals")
        if not isinstance(totals, Mapping):
            continue
        number = _finite_number(totals.get(key))
        if number is not None:
            total += number
    return total


def _llm_record_values(records: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [
        value
        for record in records
        for value in [_finite_number((record.get("llm_totals") or {}).get(key))]
        if value is not None
    ]


def _summary_group_values(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, list[float]]:
    planning_values: list[float] = []
    planning_to_execution_values: list[float] = []
    execution_values: list[float] = []
    for record in records:
        durations = record.get("action_durations")
        if not isinstance(durations, Mapping):
            continue
        planning_values.extend(durations.get("planning_duration_seconds") or [])
        planning_to_execution_values.extend(
            durations.get("planning_to_execution_start_seconds") or []
        )
        execution_values.extend(durations.get("execution_duration_seconds") or [])
    return {
        "wall_seconds": _record_values(records, "wall_seconds"),
        "task_elapsed_seconds": _record_values(records, "task_elapsed_seconds"),
        "planned_count": _record_values(records, "planned_count"),
        "executed_count": _record_values(records, "executed_count"),
        "llm_calls": _llm_record_values(records, "llm_calls"),
        "input_tokens": _llm_record_values(records, "input_tokens"),
        "output_tokens": _llm_record_values(records, "output_tokens"),
        "total_tokens": _llm_record_values(records, "total_tokens"),
        "planning_duration_seconds": planning_values,
        "planning_to_execution_start_seconds": planning_to_execution_values,
        "execution_duration_seconds": execution_values,
    }


def _average_group(records: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    return {
        key: _mean(values)
        for key, values in _summary_group_values(records).items()
    }


def _stddev_group(records: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    return {
        key: _stddev(values)
        for key, values in _summary_group_values(records).items()
    }


def _run_aggregate_metrics(
    episodes: Sequence[Any],
    *,
    started_at: float,
    finished_at: float,
) -> dict[str, Any]:
    records = [_episode_aggregate_record(episode) for episode in episodes]
    valid_records = [record for record in records if record["result_valid"]]
    successful_records = [record for record in valid_records if record["success"]]
    outcome_statuses = Counter(
        str(record["outcome_status"] or "missing") for record in records
    )
    success_count = outcome_statuses.get("success", 0)
    valid_episode_count = len(valid_records)
    return {
        "episode_count": len(records),
        "valid_episode_count": valid_episode_count,
        "successful_episode_count": len(successful_records),
        "success_count": success_count,
        "failure_count": outcome_statuses.get("failure", 0),
        "timeout_count": outcome_statuses.get("timeout", 0),
        "interrupted_count": sum(1 for record in records if record["interrupted"]),
        "success_rate": _safe_ratio(float(success_count), float(valid_episode_count)),
        "success_rate_stddev": _stddev([
            1.0 if record["success"] else 0.0
            for record in valid_records
        ]),
        "outcome_status_counts": dict(sorted(outcome_statuses.items())),
        "total_wall_seconds": max(0.0, finished_at - started_at),
        "totals": {
            "planned_count": _sum_record_values(valid_records, "planned_count"),
            "executed_count": _sum_record_values(valid_records, "executed_count"),
            "llm_calls": _sum_llm(valid_records, "llm_calls"),
            "input_tokens": _sum_llm(valid_records, "input_tokens"),
            "output_tokens": _sum_llm(valid_records, "output_tokens"),
            "total_tokens": _sum_llm(valid_records, "total_tokens"),
        },
        "all_episode_averages": _average_group(valid_records),
        "successful_episode_averages": _average_group(successful_records),
        "successful_episode_stddevs": _stddev_group(successful_records),
        "task_metric_averages": {
            "all_episodes": _average_numeric_dicts([
                record["task_metrics"] for record in valid_records
            ]),
            "successful_episodes": _average_numeric_dicts([
                record["task_metrics"] for record in successful_records
            ]),
        },
        "llm_component_token_averages": {
            "all_episodes": _average_nested_numeric_dicts([
                record["component_token_stats"] for record in valid_records
            ]),
            "successful_episodes": _average_nested_numeric_dicts([
                record["component_token_stats"] for record in successful_records
            ]),
        },
        "llm_component_token_stddevs": {
            "successful_episodes": _stddev_nested_numeric_dicts([
                record["component_token_stats"] for record in successful_records
            ]),
        },
        "error_counts": {
            "runner_error_count": sum(
                int(record["runner_error_count"]) for record in valid_records
            ),
            "worker_error_count": sum(
                int(record["worker_error_count"]) for record in valid_records
            ),
            "cleanup_error_count": sum(
                int(record["cleanup_error_count"]) for record in valid_records
            ),
        },
    }


def _coap_metrics(episodes: Sequence[Any]) -> dict[str, Any]:
    records = [_episode_aggregate_record(episode) for episode in episodes]
    valid_records = [record for record in records if record["result_valid"]]
    successful_records = [record for record in valid_records if record["success"]]
    cache_records = [
        record["cache_stats"]
        for record in valid_records
        if isinstance(record.get("cache_stats"), Mapping) and record["cache_stats"]
    ]
    successful_cache_records = [
        record["cache_stats"]
        for record in successful_records
        if isinstance(record.get("cache_stats"), Mapping) and record["cache_stats"]
    ]
    final_stats = cache_records[-1] if cache_records else {}
    final_attempt_count = _finite_number(final_stats.get("attempt_count")) or 0.0
    final_success_count = _finite_number(final_stats.get("success_count")) or 0.0
    hit_total = sum(
        value
        for stats in cache_records
        for value in [_finite_number(stats.get("hit_count"))]
        if value is not None
    )
    return {
        "final_entry_count": final_stats.get("entry_count"),
        "final_hit_count": final_stats.get("hit_count"),
        "final_attempt_count": final_stats.get("attempt_count"),
        "final_success_count": final_stats.get("success_count"),
        "cache_success_rate": _safe_ratio(final_success_count, final_attempt_count),
        "cache_hit_per_episode": _safe_ratio(hit_total, float(len(valid_records))),
        "final_buffer_size": final_stats.get("buffer_size"),
        "final_update_count": final_stats.get("update_count"),
        "all_episode_averages": _average_numeric_dicts(cache_records),
        "successful_episode_averages": _average_numeric_dicts(successful_cache_records),
    }


def _pvp_team_entries(result: Mapping[str, Any]) -> list[dict[str, str]]:
    team_results = result.get("team_results")
    if not isinstance(team_results, list):
        return []
    entries: list[dict[str, str]] = []
    for item in team_results:
        if not isinstance(item, Mapping):
            continue
        team_id = str(item.get("team_id") or "").strip()
        method = str(item.get("method") or "").strip()
        if team_id and method:
            entries.append({"team_id": team_id, "method": method})
    return entries


def _pvp_winner_team_id(result: Mapping[str, Any]) -> str | None:
    outcome = result.get("outcome")
    if not isinstance(outcome, Mapping):
        return None
    if str(outcome.get("status") or "") != "success":
        return None
    detail = outcome.get("detail")
    if not isinstance(detail, Mapping):
        return None
    winner = str(detail.get("winner") or "").strip()
    return winner or None


def _pvp_method_stats() -> dict[str, Any]:
    return {
        "match_count": 0,
        "appearance_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "no_winner_count": 0,
    }


def _pvp_metrics(episodes: Sequence[Any]) -> dict[str, Any]:
    records = [_episode_aggregate_record(episode) for episode in episodes]
    method_results: dict[str, dict[str, Any]] = {}
    method_match_ids: dict[str, set[str]] = {}
    matchups: dict[str, dict[str, Any]] = {}
    outcome_statuses: Counter[str] = Counter()
    valid_match_count = 0
    winner_count = 0
    no_winner_count = 0

    for index, record in enumerate(records):
        result = record.get("result")
        if not isinstance(result, Mapping):
            continue
        outcome_statuses[str(record.get("outcome_status") or "missing")] += 1
        entries = _pvp_team_entries(result)
        if not entries:
            continue
        valid_match_count += 1
        match_id = str(record.get("episode_id") or index)
        winner_team_id = _pvp_winner_team_id(result)
        winner_method = None
        if winner_team_id is not None:
            for entry in entries:
                if entry["team_id"] == winner_team_id:
                    winner_method = entry["method"]
                    break
        if winner_method is None:
            no_winner_count += 1
        else:
            winner_count += 1

        for entry in entries:
            method = entry["method"]
            stats = method_results.setdefault(method, _pvp_method_stats())
            method_match_ids.setdefault(method, set()).add(match_id)
            stats["appearance_count"] = int(stats["appearance_count"] or 0) + 1
            if winner_method is None:
                stats["no_winner_count"] = int(stats["no_winner_count"] or 0) + 1
            elif entry["team_id"] == winner_team_id:
                stats["win_count"] = int(stats["win_count"] or 0) + 1
            else:
                stats["loss_count"] = int(stats["loss_count"] or 0) + 1

        matchup_methods = sorted(entry["method"] for entry in entries)
        matchup_key = "_vs_".join(
            _safe_part(method, default="method") for method in matchup_methods
        )
        matchup = matchups.setdefault(
            matchup_key,
            {
                "match_count": 0,
                "methods": matchup_methods,
                "wins": {},
                "win_rates": {},
                "no_winner_count": 0,
            },
        )
        matchup["match_count"] = int(matchup["match_count"]) + 1
        wins = matchup["wins"]
        if isinstance(wins, dict):
            for method in matchup_methods:
                wins.setdefault(method, 0)
            if winner_method is None:
                matchup["no_winner_count"] = int(matchup["no_winner_count"]) + 1
            else:
                wins[winner_method] = int(wins.get(winner_method, 0)) + 1

    for method, stats in method_results.items():
        stats["match_count"] = len(method_match_ids.get(method, set()))
        appearances = float(stats["appearance_count"] or 0.0)
        stats["win_rate"] = _safe_ratio(float(stats["win_count"] or 0.0), appearances)
        stats["loss_rate"] = _safe_ratio(float(stats["loss_count"] or 0.0), appearances)
        stats["no_winner_rate"] = _safe_ratio(
            float(stats["no_winner_count"] or 0.0),
            appearances,
        )

    for matchup in matchups.values():
        match_count = float(matchup["match_count"])
        wins = matchup["wins"]
        if isinstance(wins, dict):
            matchup["wins"] = dict(sorted(wins.items()))
            matchup["win_rates"] = {
                method: _safe_ratio(float(count), match_count)
                for method, count in matchup["wins"].items()
            }

    return {
        "match_count": len(records),
        "valid_match_count": valid_match_count,
        "winner_count": winner_count,
        "no_winner_count": no_winner_count,
        "outcome_status_counts": dict(sorted(outcome_statuses.items())),
        "method_results": dict(sorted(method_results.items())),
        "matchups": dict(sorted(matchups.items())),
    }


def _normalized_usage(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(usage or {})
    normalized: dict[str, int | None] = {}
    for target, aliases in _TOKEN_ALIASES.items():
        normalized[target] = None
        for alias in aliases:
            candidate = raw.get(alias)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                normalized[target] = candidate
                break
    return {
        "raw": raw,
        "normalized": normalized,
        "usage_available": bool(raw),
    }


class LLMCallHandle:
    """One started LLM call that can be atomically finalized once."""

    def __init__(
        self,
        recorder: "LLMCallRecorder",
        *,
        path: Path,
        payload: dict[str, Any],
    ) -> None:
        self._recorder = recorder
        self.path = path
        self.payload = payload
        self.request_id = str(payload["request_id"])
        self.started_at = float(payload["started_at"])
        self._finished = False
        self._lock = threading.Lock()

    def finish(
        self,
        *,
        status: str,
        response: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        error_type: str | None = None,
        error: str | None = None,
        completed_at: float | None = None,
    ) -> Path:
        with self._lock:
            if self._finished:
                return self.path
            finished_at = time.time() if completed_at is None else completed_at
            payload = dict(self.payload)
            payload.update(
                {
                    "status": status,
                    "completed_at": finished_at,
                    "elapsed_seconds": max(0.0, finished_at - self.started_at),
                    "response": _redact(dict(response or {}), self._recorder.config),
                    "usage": _redact(_normalized_usage(usage), self._recorder.config),
                    "error": (
                        {"type": error_type, "message": error}
                        if error_type or error
                        else None
                    ),
                }
            )
            payload = _redact(payload, self._recorder.config)
            try:
                _atomic_write_json(self.path, payload)
            except Exception as exc:
                raise RecordingError(
                    f"failed to finalize LLM call {self.request_id}: {exc}"
                ) from exc
            self._recorder._on_finished(self, payload)
            self._finished = True
            return self.path


class LLMCallRecorder:
    """Record per-call evidence and aggregate provider-reported token usage."""

    def __init__(self, context: EpisodeContext, config: RecordingConfig) -> None:
        self.context = context
        self.config = config
        self.calls_dir = _ensure_within(context.path, context.path / "llm" / "calls")
        self.summary_path = _ensure_within(context.path, context.path / "llm" / "summary.json")
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active: dict[str, LLMCallHandle] = {}
        self._finished: list[dict[str, Any]] = []
        self._counters: dict[tuple[str, str], int] = {}
        self._pending_begin_writes = 0
        self._closed = False

    def begin(
        self,
        *,
        request_id: str,
        mode: str,
        source: LLMCallSource,
        request: Mapping[str, Any],
        started_at: float | None = None,
    ) -> LLMCallHandle:
        call_id = _validate_slug(request_id, "request_id")
        start = time.time() if started_at is None else started_at
        with self._condition:
            if self._closed:
                raise RecordingError("LLM call recorder is closed")
            if call_id in self._active or any(
                item.get("request_id") == call_id for item in self._finished
            ):
                raise RecordingError(f"duplicate LLM request_id: {call_id}")
            filename = self._call_filename(call_id, mode, source, started_at=start)
            path = _ensure_within(self.context.path, self.calls_dir / filename)
            if path.exists():
                raise RecordingError(f"LLM call artifact already exists: {path}")
            payload = {
                "schema_version": self.config.schema_version,
                "kind": "llm_call",
                "baseline": self.context.baseline,
                "run_id": self.context.run_id,
                "episode_id": self.context.episode_id,
                "request_id": call_id,
                "source": source.to_dict(default_operation=mode),
                "mode": mode,
                "status": "started",
                "started_at": start,
                "completed_at": None,
                "elapsed_seconds": None,
                "request": _redact(request, self.config),
                "response": None,
                "usage": _normalized_usage(None),
                "error": None,
            }
            handle = LLMCallHandle(self, path=path, payload=payload)
            self._active[call_id] = handle
            self._pending_begin_writes += 1
        try:
            _atomic_write_json(path, payload)
        except Exception as exc:
            with self._condition:
                self._active.pop(call_id, None)
                self._pending_begin_writes -= 1
                self._condition.notify_all()
            raise RecordingError(
                f"failed to start LLM call record {call_id}: {exc}"
            ) from exc
        with self._condition:
            self._pending_begin_writes -= 1
            self._condition.notify_all()
        return handle

    def _call_filename(
        self,
        call_id: str,
        mode: str,
        source: LLMCallSource,
        *,
        started_at: float,
    ) -> str:
        """Human-readable call filename with a timestamp prefix for ordering."""
        agent = _safe_part(getattr(source, "agent_id", "") or "", default="agent")
        operation = _safe_part(
            getattr(source, "operation", "") or "",
            default=_safe_part(mode, default="call"),
        )
        key = (agent, operation)
        sequence = self._counters.get(key, 0) + 1
        self._counters[key] = sequence
        timestamp = datetime.fromtimestamp(started_at).strftime(_RUN_TIMESTAMP_FORMAT)
        return f"{timestamp}.{agent}.{operation}.{sequence:03d}.{call_id[:8]}.json"

    def _on_finished(self, handle: LLMCallHandle, payload: dict[str, Any]) -> None:
        with self._lock:
            self._active.pop(handle.request_id, None)
            self._finished.append(payload)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            finished = list(self._finished)
            statuses = Counter(str(item["status"]) for item in finished)
            token_sums, token_coverage, calls_with_usage = _llm_token_totals(finished)
            total = len(self._finished) + len(self._active)
            return {
                "schema_version": self.config.schema_version,
                "kind": "llm_summary",
                "baseline": self.context.baseline,
                "run_id": self.context.run_id,
                "episode_id": self.context.episode_id,
                "total_calls": total,
                "finished_calls": len(self._finished),
                "incomplete_calls": len(self._active),
                "status_counts": dict(sorted(statuses.items())),
                "calls_with_usage": calls_with_usage,
                "calls_without_usage": len(self._finished) - calls_with_usage,
                "token_sums": token_sums,
                "token_field_coverage": token_coverage,
                "component_token_stats": _llm_component_token_stats(finished),
                "updated_at": time.time(),
            }

    def close(self) -> Path:
        with self._condition:
            if not self._closed:
                self._closed = True
            while self._pending_begin_writes:
                self._condition.wait()
            payload = self.summary()
            try:
                _atomic_write_json(self.summary_path, payload)
            except Exception as exc:
                raise RecordingError(f"failed to write LLM summary: {exc}") from exc
            return self.summary_path


class EpisodeRecorder:
    """Own all evidence files for one task episode."""

    def __init__(
        self,
        run: "RunRecorder",
        context: EpisodeContext,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        self.run = run
        self.context = context
        self.config = run.config
        self.path = context.path
        self.logs_dir = _ensure_within(self.path, self.path / "logs")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = _ensure_within(self.path, self.path / "events.jsonl")
        self.episode_path = _ensure_within(self.path, self.path / "episode.json")
        self.result_path = _ensure_within(self.path, self.path / "result.json")
        self.memory_path = _ensure_within(self.path, self.path / "memory.json")
        self.task_spec_path = _ensure_within(self.path, self.path / "task_spec.yaml")
        self.llm_calls = LLMCallRecorder(context, self.config)
        self._raw_metadata = dict(metadata or {})
        self._metadata = _redact(self._raw_metadata, self.config)
        self._started_at = time.time()
        self._finished_at: float | None = None
        self._status = "started"
        self._sequence = 0
        self._lock = threading.RLock()
        self._write_task_spec()
        self._write_manifest()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def event(self, event_type: str, **data: Any) -> None:
        with self._lock:
            if self._finished_at is not None:
                return
            self._sequence += 1
            payload = _redact(
                {
                    "schema_version": self.config.schema_version,
                    "sequence": self._sequence,
                    "created_at": time.time(),
                    "type": str(event_type),
                    "data": data,
                },
                self.config,
            )
            try:
                with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception as exc:
                raise RecordingError(f"failed to append episode event: {exc}") from exc

    def write_result(self, result: Any) -> Path:
        payload = {
            "schema_version": self.config.schema_version,
            "kind": "episode_result",
            "result": _redact(_json_value(result), self.config),
        }
        _atomic_write_json(self.result_path, payload)
        return self.result_path

    def write_memory(self, memory: Any) -> Path:
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        payload = {
            "schema_version": self.config.schema_version,
            "kind": "team_memory",
            "entries": _redact(_json_value(snapshot), self.config),
        }
        _atomic_write_json(self.memory_path, payload)
        return self.memory_path

    def update_metadata(self, **metadata: Any) -> None:
        with self._lock:
            if self._finished_at is not None:
                raise RecordingError("episode recorder is closed")
            self._metadata.update(_redact(metadata, self.config))
            self._write_manifest()

    def team_episode(
        self,
        team_id: str,
        *,
        baseline: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TeamEpisodeRecorder":
        """Create an episode-shaped child recorder under this episode path."""
        identifier = _validate_slug(team_id, "episode_id")
        path = _ensure_within(self.path, self.path / identifier)
        try:
            path.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise RecordingError(f"duplicate team episode: {identifier}") from exc
        context = EpisodeContext(
            baseline=(
                _validate_slug(baseline, "baseline")
                if baseline is not None
                else self.context.baseline
            ),
            run_id=self.context.run_id,
            episode_id=f"{self.context.episode_id}.{identifier}",
            path=path,
        )
        child_metadata = {
            "parent_episode_id": self.context.episode_id,
            "team_id": identifier,
            **dict(metadata or {}),
        }
        return TeamEpisodeRecorder(self, context, child_metadata)

    def finalize(
        self,
        *,
        status: str,
        result: Any | None = None,
        memory: Any | None = None,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        with self._lock:
            if self._finished_at is not None:
                return self.path
            if result is not None:
                self.write_result(result)
            if memory is not None:
                self.write_memory(memory)
            self.llm_calls.close()
            self._status = str(status)
            self._finished_at = time.time()
            if metadata:
                self._metadata.update(_redact(dict(metadata), self.config))
            if error:
                self._metadata["error"] = _redact(str(error), self.config)
            self._write_manifest()
            self.run._episode_finished(self)
            return self.path

    def _write_manifest(self) -> None:
        payload = {
            "schema_version": self.config.schema_version,
            "kind": "episode",
            "baseline": self.context.baseline,
            "run_id": self.context.run_id,
            "episode_id": self.context.episode_id,
            "status": self._status,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "metadata": self._metadata,
        }
        _atomic_write_json(self.episode_path, payload)

    def _write_task_spec(self) -> None:
        task_spec = self._raw_metadata.get("task_spec")
        if task_spec is None:
            return
        payload = _json_value(task_spec)
        try:
            _atomic_write_yaml(self.task_spec_path, payload)
        except Exception as exc:  # noqa: BLE001
            raise RecordingError(f"failed to write task spec yaml: {exc}") from exc


class TeamEpisodeRecorder(EpisodeRecorder):
    """Episode-shaped child recorder that does not increment run episode count."""

    def __init__(
        self,
        parent: EpisodeRecorder,
        context: EpisodeContext,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        self.parent = parent
        super().__init__(parent.run, context, metadata)


class RunRecorder:
    """Manage sequential episodes under one baseline run."""

    def __init__(
        self,
        context: RunContext,
        config: RecordingConfig,
        metadata: Mapping[str, Any] | None,
        *,
        write_manifest: bool = True,
    ) -> None:
        self.context = context
        self.config = config
        self.path = context.path
        self.run_path = _ensure_within(self.path, self.path / "run.json")
        self.summary_path = _ensure_within(self.path, self.path / "summary.json")
        self._metadata = _redact(dict(metadata or {}), config)
        self._started_at = time.time()
        self._finished_at: float | None = None
        self._status = "started"
        self._episodes: list[EpisodeRecorder] = []
        self._lock = threading.RLock()
        if write_manifest:
            self._write_manifest()

    def start_episode(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        episode_id: str | None = None,
    ) -> EpisodeRecorder:
        with self._lock:
            if self._finished_at is not None:
                raise RecordingError("run recorder is closed")
            episode_metadata = dict(metadata or {})
            if not self._episodes and "config" in episode_metadata:
                self._metadata.setdefault(
                    "config",
                    _redact(episode_metadata["config"], self.config),
                )
            identifier = episode_id or f"episode-{len(self._episodes) + 1:04d}"
            identifier = _validate_slug(identifier, "episode_id")
            path = _ensure_within(self.path, self.path / identifier)
            try:
                path.mkdir(parents=False, exist_ok=False)
            except FileExistsError as exc:
                raise RecordingError(f"duplicate episode_id: {identifier}") from exc
            context = EpisodeContext(
                baseline=self.context.baseline,
                run_id=self.context.run_id,
                episode_id=identifier,
                path=path,
            )
            episode = EpisodeRecorder(self, context, episode_metadata)
            self._episodes.append(episode)
            self._write_manifest()
            return episode

    def retry_episode(
        self,
        episode: EpisodeRecorder,
        *,
        attempt: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> EpisodeRecorder:
        """Archive one failed attempt and recreate its canonical episode path.

        The run manifest keeps one entry for the logical episode.  The failed
        attempt is moved below ``<episode>/attempt-N`` so its evidence remains
        available without making the next retry look like a new episode.
        """
        if attempt < 1:
            raise ValueError("attempt 必须 >= 1")
        with self._lock:
            if self._finished_at is not None:
                raise RecordingError("run recorder is closed")
            try:
                episode_index = self._episodes.index(episode)
            except ValueError as exc:
                raise RecordingError("episode 不属于当前 run recorder") from exc

            identifier = _validate_slug(episode.context.episode_id, "episode_id")
            path = _ensure_within(self.path, self.path / identifier)
            archive_name = f"attempt-{attempt}"
            archive_path = _ensure_within(path, path / archive_name)
            if path != episode.path.resolve() or not path.is_dir():
                raise RecordingError(f"episode 目录不存在: {path}")
            if archive_path.exists():
                raise RecordingError(f"重复的 episode 尝试归档: {archive_path}")

            staging_path = _ensure_within(
                self.path,
                self.path / f".{identifier}.retry-{uuid4().hex}",
            )
            path.rename(staging_path)
            try:
                path.mkdir(parents=False, exist_ok=False)
                staging_path.rename(path / archive_name)
            except Exception:
                # Restore the canonical path when the directory move fails
                # before a new recorder can be constructed.
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
                if staging_path.is_dir() and not path.exists():
                    staging_path.rename(path)
                raise

            retry_metadata = dict(metadata or {})
            retry_metadata.setdefault("retry_attempt", attempt + 1)
            retry_metadata.setdefault("previous_attempt", attempt)
            context = EpisodeContext(
                baseline=self.context.baseline,
                run_id=self.context.run_id,
                episode_id=identifier,
                path=path,
            )
            replacement = EpisodeRecorder(self, context, retry_metadata)
            self._episodes[episode_index] = replacement
            self._write_manifest()
            return replacement

    def _episode_finished(self, _: EpisodeRecorder) -> None:
        with self._lock:
            self._write_manifest()

    @property
    def episode_count(self) -> int:
        """当前 run 中已创建（含已 finalize）的 episode 数量。"""
        with self._lock:
            return len(self._episodes)

    @property
    def metadata(self) -> dict[str, Any]:
        """返回 run metadata 的副本，供续跑恢复原始运行配置。"""
        with self._lock:
            return dict(self._metadata)

    def config_values(self) -> dict[str, Any]:
        """返回首次 episode 保存的运行配置。"""
        value = self.metadata.get("config")
        if not isinstance(value, Mapping):
            raise RecordingError("run 记录缺少可恢复的 config")
        return dict(value)

    def update_metadata(self, **metadata: Any) -> None:
        """向 run 清单追加 metadata 并重写 run.json（用于续跑标记等）。"""
        with self._lock:
            self._metadata.update(_redact(metadata, self.config))
            self._write_manifest()

    @classmethod
    def open(cls, context: RunContext, config: RecordingConfig) -> "RunRecorder":
        """从已存在的 run.json 打开一个 run，恢复已有 episode 后允许继续追加。

        用于"跳过已完成 episode 续跑"：恢复的旧 episode 以轻量占位对象保留
        （episode_id + 终态 status），新增 episode 正常追加；finalize 时
        summary.json 会覆盖为包含新旧 episode 的完整列表。
        """
        run_path = _ensure_within(context.path, context.path / "run.json")
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RecordingError(f"无法读取 run 清单: {run_path}: {exc}") from exc
        if not isinstance(payload, Mapping) or payload.get("kind") != "run":
            raise RecordingError(f"不是有效的 run 清单: {run_path}")
        if (
            payload.get("baseline") != context.baseline
            or payload.get("run_id") != context.run_id
        ):
            raise RecordingError("run 清单与目录上下文不匹配")
        episodes = payload.get("episodes")
        if not isinstance(episodes, list):
            raise RecordingError(f"run 清单 episodes 无效: {run_path}")
        restored: list[_EpisodeStub] = []
        for item in episodes:
            if not isinstance(item, Mapping) or "episode_id" not in item:
                raise RecordingError(f"run 清单 episode 条目无效: {item!r}")
            restored.append(
                _EpisodeStub(
                    episode_id=str(item["episode_id"]),
                    status=str(item.get("status") or "incomplete"),
                    path=context.path / str(item["episode_id"]),
                )
            )
        instance = cls(
            context,
            config,
            metadata=payload.get("metadata"),
            write_manifest=False,
        )
        started_at = payload.get("started_at")
        if isinstance(started_at, (int, float)):
            instance._started_at = float(started_at)
        instance._episodes = restored
        instance._status = "started"
        instance._finished_at = None
        instance._write_manifest()
        return instance

    def finalize(self, *, status: str | None = None) -> Path:
        with self._lock:
            if self._finished_at is not None:
                return self.path
            for episode in self._episodes:
                if episode.status == "started":
                    episode.finalize(status="incomplete", error="run finalized before episode")
            statuses = [episode.status for episode in self._episodes]
            if status is None:
                status = (
                    "completed"
                    if statuses and all(item == "completed" for item in statuses)
                    else "failed"
                )
            self._status = status
            self._finished_at = time.time()
            self._write_manifest()
            summary = {
                "schema_version": self.config.schema_version,
                "kind": "run_summary",
                "baseline": self.context.baseline,
                "run_id": self.context.run_id,
                "status": self._status,
                "episode_count": len(self._episodes),
                "episode_status_counts": dict(
                    sorted(Counter(episode.status for episode in self._episodes).items())
                ),
                "episodes": [
                    {
                        "episode_id": episode.context.episode_id,
                        "status": episode.status,
                        "path": str(episode.path),
                    }
                    for episode in self._episodes
                ],
                "started_at": self._started_at,
                "finished_at": self._finished_at,
            }
            summary["aggregate_metrics"] = _run_aggregate_metrics(
                self._episodes,
                started_at=self._started_at,
                finished_at=self._finished_at,
            )
            baseline_key = self.context.baseline.lower()
            if baseline_key == "coap":
                summary["coap_metrics"] = _coap_metrics(self._episodes)
            if baseline_key == "pvp":
                summary["pvp_metrics"] = _pvp_metrics(self._episodes)
        _atomic_write_json(self.summary_path, summary)
        return self.path

    def _write_manifest(self) -> None:
        payload = {
            "schema_version": self.config.schema_version,
            "kind": "run",
            "baseline": self.context.baseline,
            "run_id": self.context.run_id,
            "status": self._status,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "metadata": self._metadata,
            "episodes": [
                {"episode_id": episode.context.episode_id, "status": episode.status}
                for episode in self._episodes
            ],
        }
        _atomic_write_json(self.run_path, payload)


class RecordingManager:
    """Create validated run directories under a single recording root."""

    def __init__(self, config: RecordingConfig | None = None) -> None:
        self.config = config or RecordingConfig()
        self.root = self.config.root_dir.resolve()
        self._lock = threading.Lock()

    def start_run(
        self,
        baseline: str,
        *,
        task: str | None = None,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunRecorder:
        method = _validate_slug(baseline, "baseline")
        with self._lock:
            baseline_dir = _ensure_within(self.root, self.root / method)
            baseline_dir.mkdir(parents=True, exist_ok=True)
            parent_dir = baseline_dir
            task_slug = None
            if task:
                task_slug = _safe_part(task, default="task", limit=80)
                parent_dir = _ensure_within(
                    self.root,
                    baseline_dir / task_slug,
                )
                parent_dir.mkdir(parents=True, exist_ok=True)
            generated = run_id is None
            identifier = run_id or datetime.now().strftime(_RUN_TIMESTAMP_FORMAT)
            identifier = _validate_slug(identifier, "run_id")
            candidate = identifier
            suffix = 2
            while True:
                path = _ensure_within(self.root, parent_dir / candidate)
                try:
                    path.mkdir(parents=False, exist_ok=False)
                    break
                except FileExistsError:
                    if not generated:
                        raise RecordingError(f"duplicate run_id: {identifier}")
                    candidate = f"{identifier}-{suffix}"
                    suffix += 1
            context = RunContext(
                baseline=method,
                run_id=candidate,
                path=path,
                task=task_slug,
            )
            return RunRecorder(context, self.config, metadata)

    def open_run(
        self,
        baseline: str,
        *,
        task: str | None = None,
        run_id: str,
    ) -> RunRecorder:
        """打开已存在的 run（按 baseline/task/run_id 定位）以继续追加 episode。"""
        method = _validate_slug(baseline, "baseline")
        if not run_id or not str(run_id).strip():
            raise RecordingError("open_run 需要非空 run_id")
        identifier = _validate_slug(run_id, "run_id")
        with self._lock:
            baseline_dir = _ensure_within(self.root, self.root / method)
            parent_dir = baseline_dir
            task_slug = None
            if task:
                task_slug = _safe_part(task, default="task", limit=80)
                parent_dir = _ensure_within(
                    self.root,
                    baseline_dir / task_slug,
                )
            path = _ensure_within(self.root, parent_dir / identifier)
            if not path.is_dir():
                raise RecordingError(f"run 目录不存在: {path}")
            if not (path / "run.json").is_file():
                raise RecordingError(f"run 缺少 run.json: {path}")
            context = RunContext(
                baseline=method,
                run_id=identifier,
                path=path,
                task=task_slug,
            )
            return RunRecorder.open(context, self.config)


class _EpisodeStub:
    """RunRecorder.open 恢复用的轻量 episode 占位。"""

    def __init__(
        self,
        *,
        episode_id: str,
        status: str,
        path: str | Path | None = None,
    ) -> None:
        self.context = SimpleNamespace(episode_id=episode_id)
        self.status = status
        self.path = Path(path) if path is not None else None

    def finalize(self, *, status: str | None = None, error: str | None = None) -> None:
        """旧 episode 已终态，无需再 finalize。"""
        return None
