"""LLM output protocol parsing for the AutoGen baseline."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

from .schemas import ExecutorDecision, SubtaskAssignment


class AutoGenProtocolError(ValueError):
    """Raised when an AutoGen baseline LLM response is invalid."""


@dataclass(frozen=True)
class CommanderParseResult:
    reasoning: str
    assignments: tuple[SubtaskAssignment, ...]
    raw_text: str


_REASON_RE = re.compile(r"<reason>(.*?)</reason>", re.DOTALL)
_ASSIGNMENTS_RE = re.compile(r"<assignments>(.*?)</assignments>", re.DOTALL)
_STATUS_RE = re.compile(r"<status>(.*?)</status>", re.DOTALL)
_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL)
_REPORT_RE = re.compile(r"<report>(.*?)</report>", re.DOTALL)


def parse_commander_response(
    text: str,
    *,
    eligible_recipients: Sequence[str],
    default_max_rounds: int = 5,
) -> CommanderParseResult:
    source = _clean_text(text)
    reasoning = _required_tag(_REASON_RE, source, "reason")
    raw_json = _required_tag(_ASSIGNMENTS_RE, source, "assignments")
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AutoGenProtocolError(f"assignments must be valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise AutoGenProtocolError("assignments must be a JSON array")
    eligible = set(eligible_recipients)
    assignments: list[SubtaskAssignment] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AutoGenProtocolError(f"assignment {index} must be an object")
        try:
            assignment = SubtaskAssignment.from_dict(
                item,
                default_max_rounds=default_max_rounds,
            )
        except (TypeError, ValueError) as exc:
            raise AutoGenProtocolError(f"assignment {index} invalid: {exc}") from exc
        if assignment.recipient not in eligible:
            raise AutoGenProtocolError(
                f"assignment recipient is not eligible: {assignment.recipient}"
            )
        if assignment.recipient in seen:
            raise AutoGenProtocolError(
                f"assignment recipient appears more than once: {assignment.recipient}"
            )
        seen.add(assignment.recipient)
        assignments.append(assignment)
    return CommanderParseResult(
        reasoning=reasoning,
        assignments=tuple(assignments),
        raw_text=source,
    )


def parse_executor_response(text: str) -> ExecutorDecision:
    source = _clean_text(text)
    status = _required_tag(_STATUS_RE, source, "status").strip().lower()
    if status not in {"continue", "completed", "failed", "blocked"}:
        raise AutoGenProtocolError(f"invalid executor status: {status!r}")
    reasoning = _required_tag(_REASON_RE, source, "reason")
    action_matches = _ACTION_RE.findall(source)
    report_matches = _REPORT_RE.findall(source)
    if status == "continue":
        if len(action_matches) != 1:
            raise AutoGenProtocolError(
                "continue response must contain exactly one action"
            )
        if report_matches:
            raise AutoGenProtocolError("continue response must not contain report")
        action_text = str(action_matches[0]).strip()
        if not action_text:
            raise AutoGenProtocolError("action must not be empty")
        return ExecutorDecision(
            status=status,
            reason=reasoning,
            action_text=action_text,
            raw_text=source,
        )
    if action_matches:
        raise AutoGenProtocolError("terminal response must not contain action")
    if len(report_matches) != 1:
        raise AutoGenProtocolError(
            "terminal response must contain exactly one report"
        )
    report = str(report_matches[0]).strip()
    if not report:
        raise AutoGenProtocolError("report must not be empty")
    return ExecutorDecision(
        status=status,
        reason=reasoning,
        report=report,
        raw_text=source,
    )


def _required_tag(pattern: re.Pattern[str], source: str, label: str) -> str:
    matches = pattern.findall(source)
    if len(matches) != 1:
        raise AutoGenProtocolError(
            f"response must contain exactly one <{label}>...</{label}>"
        )
    value = str(matches[0]).strip()
    if not value:
        raise AutoGenProtocolError(f"{label} must not be empty")
    return value


def _clean_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise AutoGenProtocolError("model response must not be empty")
    source = text.strip()
    if source.startswith("```") and source.endswith("```"):
        lines = source.splitlines()
        if len(lines) >= 3:
            source = "\n".join(lines[1:-1]).strip()
    return source


__all__ = [
    "AutoGenProtocolError",
    "CommanderParseResult",
    "parse_commander_response",
    "parse_executor_response",
]
