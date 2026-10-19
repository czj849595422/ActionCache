"""AgenticCache transition cache.

This is intentionally separate from COAP.  It implements the paper's local
2-gram cache idea: previous action -> next action, filtered by task-state
metadata ranges and ranked by transition_count * importance.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from typing import Any, Iterable, Sequence

from .schemas import (
    ActionPlan,
    AgenticCacheEntry,
    CacheLookupResult,
    MetadataRange,
)
from .templates import migrate_v1_plan_to_template


class AgenticCacheError(RuntimeError):
    pass


def canonical_action_key(action: ActionPlan, *, include_parameters: bool = True) -> str:
    payload: dict[str, Any] = {"name": action.name}
    if include_parameters:
        payload["parameters"] = action.parameters
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class AgenticCacheStore:
    """Thread-safe per-agent transition cache."""

    def __init__(
        self,
        *,
        top_k: int = 5,
        metadata_tolerance: float = 0.0,
        min_transition_count: int = 1,
        match_previous_parameters: bool = False,
        instructions: Any | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k 必须 >= 1")
        if min_transition_count < 1:
            raise ValueError("min_transition_count 必须 >= 1")
        self.top_k = int(top_k)
        self.metadata_tolerance = float(metadata_tolerance)
        self.min_transition_count = int(min_transition_count)
        self.match_previous_parameters = bool(match_previous_parameters)
        self.instructions = instructions
        self._entries: dict[str, AgenticCacheEntry] = {}
        self._lock = threading.RLock()

    def load_file(self, path: str | Path) -> int:
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise AgenticCacheError("cache source must be a JSON object")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise AgenticCacheError("cache source must contain entries[]")
        schema_version = int(payload.get("schema_version") or 1)
        entries: list[AgenticCacheEntry] = []
        for item in raw_entries:
            try:
                entry = AgenticCacheEntry.from_dict(item)
                if schema_version < 2:
                    entry = self._migrate_v1_entry(entry)
                entries.append(entry)
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            for entry in entries:
                self._entries[entry.entry_id] = entry
        return len(entries)

    def upsert_success(
        self,
        *,
        previous_action: ActionPlan,
        next_action: ActionPlan,
        state_metadata: dict[str, float],
        owner_agent_id: str,
        reason: str = "",
        source: str = "online",
    ) -> AgenticCacheEntry:
        with self._lock:
            existing = self._find_pair(
                previous_action,
                next_action,
                owner_agent_id=owner_agent_id,
            )
            now = time.time()
            if existing is None:
                entry = AgenticCacheEntry(
                    previous_action=deepcopy(previous_action),
                    next_action=deepcopy(next_action),
                    metadata_ranges=_ranges_from_metadata(state_metadata),
                    transition_count=1,
                    candidate_count=1,
                    confirmed_count=1,
                    reason=reason,
                    source=source,
                    owner_agent_id=owner_agent_id,
                    created_at=now,
                    updated_at=now,
                )
                self._entries[entry.entry_id] = entry
                return deepcopy(entry)
            existing.transition_count += 1
            existing.candidate_count = max(existing.candidate_count, 1)
            existing.confirmed_count += 1
            existing.reason = reason or existing.reason
            existing.source = source or existing.source
            _merge_ranges(existing.metadata_ranges, state_metadata)
            existing.updated_at = now
            return deepcopy(existing)

    def reject_entry(self, entry_id: str) -> AgenticCacheEntry | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            entry.failure_count += 1
            entry.updated_at = time.time()
            return deepcopy(entry)

    def lookup(
        self,
        previous_action: ActionPlan,
        state_metadata: dict[str, float],
        *,
        owner_agent_id: str,
        top_k: int | None = None,
    ) -> CacheLookupResult:
        limit = self.top_k if top_k is None else max(1, int(top_k))
        with self._lock:
            matched = [
                entry
                for entry in self._entries.values()
                if entry.owner_agent_id == owner_agent_id
                and entry.transition_count >= self.min_transition_count
                and self._previous_matches(entry.previous_action, previous_action)
            ]
            feasible = [
                entry
                for entry in matched
                if _metadata_matches(
                    entry.metadata_ranges,
                    state_metadata,
                    tolerance=self.metadata_tolerance,
                )
            ]
            for entry in feasible:
                entry.candidate_count += 1
                entry.updated_at = time.time()
            ranked = sorted(
                feasible,
                key=lambda entry: (
                    -entry.score,
                    -entry.transition_count,
                    -entry.confirmed_count,
                    entry.entry_id,
                ),
            )
            selected = tuple(deepcopy(entry) for entry in ranked[:limit])
            return CacheLookupResult(
                previous_action=deepcopy(previous_action),
                entries=selected,
                matched_count=len(matched),
                filtered_count=max(0, len(matched) - len(feasible)),
            )

    def list_entries(
        self,
        *,
        owner_agent_id: str | None = None,
    ) -> tuple[AgenticCacheEntry, ...]:
        with self._lock:
            values: Iterable[AgenticCacheEntry] = self._entries.values()
            if owner_agent_id is not None:
                values = (
                    entry
                    for entry in values
                    if entry.owner_agent_id == owner_agent_id
                )
            return tuple(deepcopy(entry) for entry in values)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries.values())
            return {
                "entry_count": len(entries),
                "transition_count": sum(entry.transition_count for entry in entries),
                "candidate_count": sum(entry.candidate_count for entry in entries),
                "confirmed_count": sum(entry.confirmed_count for entry in entries),
                "failure_count": sum(entry.failure_count for entry in entries),
            }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            entries = [
                entry.to_dict()
                for entry in sorted(
                    self._entries.values(),
                    key=lambda item: (item.owner_agent_id or "", item.entry_id),
                )
            ]
            return {
                "schema_version": 2,
                "kind": "agentic_cache",
                "entry_count": len(entries),
                "entries": entries,
            }

    def _migrate_v1_entry(self, entry: AgenticCacheEntry) -> AgenticCacheEntry:
        previous = migrate_v1_plan_to_template(
            entry.previous_action,
            instructions=self.instructions,
        )
        next_action = migrate_v1_plan_to_template(
            entry.next_action,
            instructions=self.instructions,
        )
        if previous is None or next_action is None:
            raise ValueError("cannot migrate v1 AgenticCache action to shorthand")
        entry.previous_action = previous
        entry.next_action = next_action
        return entry

    def _find_pair(
        self,
        previous_action: ActionPlan,
        next_action: ActionPlan,
        *,
        owner_agent_id: str,
    ) -> AgenticCacheEntry | None:
        previous_key = canonical_action_key(
            previous_action,
            include_parameters=self.match_previous_parameters,
        )
        next_key = canonical_action_key(next_action, include_parameters=True)
        for entry in self._entries.values():
            if entry.owner_agent_id != owner_agent_id:
                continue
            if (
                canonical_action_key(
                    entry.previous_action,
                    include_parameters=self.match_previous_parameters,
                )
                == previous_key
                and canonical_action_key(entry.next_action, include_parameters=True)
                == next_key
            ):
                return entry
        return None

    def _previous_matches(self, left: ActionPlan, right: ActionPlan) -> bool:
        return canonical_action_key(
            left,
            include_parameters=self.match_previous_parameters,
        ) == canonical_action_key(
            right,
            include_parameters=self.match_previous_parameters,
        )


def _ranges_from_metadata(values: dict[str, float]) -> dict[str, MetadataRange]:
    return {
        str(key): MetadataRange(float(value), float(value))
        for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _merge_ranges(
    ranges: dict[str, MetadataRange],
    values: dict[str, float],
) -> None:
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        key = str(key)
        if key not in ranges:
            ranges[key] = MetadataRange(float(value), float(value))
        else:
            ranges[key].include(float(value))


def _metadata_matches(
    ranges: dict[str, MetadataRange],
    values: dict[str, float],
    *,
    tolerance: float,
) -> bool:
    for key, value_range in ranges.items():
        value = values.get(key)
        if value is None:
            continue
        if not value_range.contains(float(value), tolerance=tolerance):
            return False
    return True


def write_cache_snapshot(path: str | Path, store: AgenticCacheStore) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(store.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(target)


__all__ = [
    "AgenticCacheError",
    "AgenticCacheStore",
    "canonical_action_key",
    "write_cache_snapshot",
]
