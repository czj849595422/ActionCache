"""Debug Runner 的稳定结果与 Prompt 快照数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_jsonable(value: Any) -> Any:
    """把调试结果转换为可持久化 JSON，未知对象仅保留字符串表示。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return str(value)


@dataclass
class DebugResult:
    """所有 Debug Controller 调用共用的响应信封。"""

    ok: bool
    operation: str
    timestamp: str = field(default_factory=utc_timestamp)
    result: Any = None
    error: dict[str, Any] | None = None
    artifact_path: str | None = None

    @classmethod
    def success(cls, operation: str, result: Any = None) -> "DebugResult":
        return cls(ok=True, operation=operation, result=result)

    @classmethod
    def failure(
        cls,
        operation: str,
        code: str,
        message: str,
        *,
        details: Any = None,
    ) -> "DebugResult":
        error: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            error["details"] = to_jsonable(details)
        return cls(ok=False, operation=operation, error=error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "result": to_jsonable(self.result),
            "error": to_jsonable(self.error),
            "artifact_path": self.artifact_path,
        }


@dataclass
class PromptSnapshot:
    """真实 Prompt builder 的只读输出，不触发模型调用。"""

    agent: str
    provider: str
    method: str
    messages: list[dict[str, Any]]
    model_parameters: dict[str, Any] = field(default_factory=dict)
    input_sources: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=utc_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(asdict(self))


class DebugOperationError(RuntimeError):
    """可安全返回给 REPL 的预期操作错误。"""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details
