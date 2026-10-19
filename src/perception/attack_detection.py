"""Track self-hurt events and health changes between successful actions."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import threading
import time
from typing import Any, Mapping


@dataclass(frozen=True)
class AttackDetectionSnapshot:
    """The latest non-prompt attack and health-change state for one agent."""

    observed_event_sequence: int | None = None
    observed_at: float | None = None
    last_attack_at: float | None = None
    baseline_event_sequence: int | None = None
    baseline_at: float | None = None
    received_attack: bool | None = None
    received_attack_count: int | None = None
    observed_health: float | None = None
    baseline_health: float | None = None
    health_delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_event_sequence": self.observed_event_sequence,
            "observed_at": self.observed_at,
            "last_attack_at": self.last_attack_at,
            "baseline_event_sequence": self.baseline_event_sequence,
            "baseline_at": self.baseline_at,
            "received_attack": self.received_attack,
            "received_attack_count": self.received_attack_count,
            "observed_health": self.observed_health,
            "baseline_health": self.baseline_health,
            "health_delta": self.health_delta,
        }


def _self_state(observation: Any) -> Mapping[str, Any]:
    if not isinstance(observation, Mapping):
        return {}
    direct = observation.get("self")
    if isinstance(direct, Mapping):
        return direct
    sections = observation.get("sections")
    if isinstance(sections, Mapping):
        self_section = sections.get("self_state")
        if isinstance(self_section, Mapping):
            facts = self_section.get("facts")
            if isinstance(facts, list) and facts and isinstance(facts[0], Mapping):
                return facts[0]
    return {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AttackDetectionTracker:
    """Compare hurt-event counters across observation/action boundaries."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current = AttackDetectionSnapshot()

    def observe(
        self,
        observation: Any,
        *,
        observed_at: float | None = None,
    ) -> AttackDetectionSnapshot:
        state = _self_state(observation)
        sequence = _int_or_none(
            state.get("attack_event_sequence", state.get("attackEventSequence"))
        )
        last_attack_at = _float_or_none(
            state.get("last_attack_at", state.get("lastAttackAt"))
        )
        health = _float_or_none(state.get("health"))
        timestamp = time.time() if observed_at is None else float(observed_at)
        with self._lock:
            baseline = self._current.baseline_event_sequence
            baseline_health = self._current.baseline_health
            baseline_at = self._current.baseline_at
            if baseline is None and sequence is not None:
                baseline = sequence
                if baseline_at is None:
                    baseline_at = timestamp
            if baseline_health is None and health is not None:
                baseline_health = health
                if baseline_at is None:
                    baseline_at = timestamp
            attack_count = _attack_count(sequence, baseline)
            received_attack = (
                None
                if baseline is None or sequence is None
                else sequence > baseline
            )
            health_delta = _health_delta(health, baseline_health)
            self._current = AttackDetectionSnapshot(
                observed_event_sequence=sequence,
                observed_at=timestamp,
                last_attack_at=last_attack_at,
                baseline_event_sequence=baseline,
                baseline_at=baseline_at,
                received_attack=received_attack,
                received_attack_count=attack_count,
                observed_health=health,
                baseline_health=baseline_health,
                health_delta=health_delta,
            )
            return self._current

    def mark_success(self) -> AttackDetectionSnapshot:
        """Start a new interval at the latest observed event sequence."""
        with self._lock:
            now = time.time()
            sequence = self._current.observed_event_sequence
            health = self._current.observed_health
            self._current = AttackDetectionSnapshot(
                observed_event_sequence=sequence,
                observed_at=self._current.observed_at,
                last_attack_at=self._current.last_attack_at,
                baseline_event_sequence=sequence,
                baseline_at=now,
                received_attack=False if sequence is not None else None,
                received_attack_count=0 if sequence is not None else None,
                observed_health=health,
                baseline_health=health,
                health_delta=0.0 if health is not None else None,
            )
            return self._current

    def snapshot(self) -> AttackDetectionSnapshot:
        with self._lock:
            return self._current


def _health_delta(
    observed_health: float | None,
    baseline_health: float | None,
) -> float | None:
    if observed_health is None or baseline_health is None:
        return None
    return round(observed_health - baseline_health, 6)


def _attack_count(
    observed_sequence: int | None,
    baseline_sequence: int | None,
) -> int | None:
    if observed_sequence is None or baseline_sequence is None:
        return None
    return max(0, observed_sequence - baseline_sequence)


def with_detection_context(
    observation: Any,
    snapshot: AttackDetectionSnapshot,
) -> Any:
    """Add display-only detection values without mutating the observation."""
    if not isinstance(observation, Mapping):
        return observation
    enriched = deepcopy(observation)
    raw_self = enriched.get("self")
    if isinstance(raw_self, dict):
        _add_raw_detection_fields(raw_self, snapshot)
    sections = enriched.get("sections")
    if isinstance(sections, dict):
        self_section = sections.get("self_state")
        if isinstance(self_section, dict):
            facts = self_section.get("facts")
            if isinstance(facts, list) and facts and isinstance(facts[0], dict):
                _add_structured_detection_fields(facts[0], snapshot)
    return enriched


def _add_raw_detection_fields(
    self_state: dict[str, Any],
    snapshot: AttackDetectionSnapshot,
) -> None:
    if snapshot.health_delta is not None:
        self_state["healthDelta"] = snapshot.health_delta
    if snapshot.received_attack_count is not None:
        self_state["recentAttackCount"] = snapshot.received_attack_count


def _add_structured_detection_fields(
    self_fact: dict[str, Any],
    snapshot: AttackDetectionSnapshot,
) -> None:
    if snapshot.health_delta is not None:
        self_fact["health_delta"] = snapshot.health_delta
    if snapshot.received_attack_count is not None:
        self_fact["recent_attack_count"] = snapshot.received_attack_count


_TRACKER_ATTR = "_mcagent_attack_detection_tracker"
_TRACKER_LOCK = threading.RLock()


def tracker_for(owner: Any) -> AttackDetectionTracker:
    """Return the tracker shared by observation and acting workers."""
    if owner is None:
        return AttackDetectionTracker()
    with _TRACKER_LOCK:
        tracker = getattr(owner, _TRACKER_ATTR, None)
        if tracker is None:
            tracker = AttackDetectionTracker()
            try:
                setattr(owner, _TRACKER_ATTR, tracker)
            except (AttributeError, TypeError):
                return tracker
        if not isinstance(tracker, AttackDetectionTracker):
            raise TypeError(f"{_TRACKER_ATTR} must be an AttackDetectionTracker")
        return tracker


def is_successful_execution(status: str, result: Mapping[str, Any]) -> bool:
    """Match the existing successful-action boundary without changing it."""
    return bool(result.get("ok")) and status not in {"failed", "interrupted"}


__all__ = [
    "AttackDetectionSnapshot",
    "AttackDetectionTracker",
    "is_successful_execution",
    "tracker_for",
    "with_detection_context",
]
