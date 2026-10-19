"""LLM 调用模块共享数据结构与错误类型。

设计参照 D:\\MC\\MCAgent\\framework\\arc\\models.py 的公开协议，但保持自包含，
不依赖 MCAgent 代码。模型输出按不透明文本处理：本模块不做动作解析或语义校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence
from uuid import uuid4

from src.recording import LLMCallSource


class GenerationMode(str, Enum):
    COMPLETE = "complete"
    STREAM = "stream"


class StreamStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class StreamEventType(str, Enum):
    STARTED = "started"
    CONTENT_DELTA = "content_delta"
    REASONING_DELTA = "reasoning_delta"
    KEYWORD_REACHED = "keyword_reached"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class LLMError(RuntimeError):
    """LLM 调用错误基类。"""

    def __init__(self, message: str, *, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


class LLMConfigurationError(LLMError):
    """配置错误。"""


class LLMRequestError(LLMError):
    """请求/响应错误。"""


class LLMRequestCancelled(LLMError):
    """请求被调用方主动取消。"""


class LLMTimeoutError(LLMError):
    """调用超时。"""


class LLMStreamError(LLMError):
    """流式调用错误。"""


class TriggerNotFoundError(LLMStreamError):
    """流结束后指定关键词未出现。"""


@dataclass(slots=True)
class LLMRequest:
    """一次 OpenAI 兼容 chat completions 请求。"""

    messages: Sequence[Mapping[str, Any]]
    request_id: str = field(default_factory=lambda: uuid4().hex)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: LLMCallSource | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise LLMConfigurationError("request_id must not be empty")
        if not self.messages:
            raise LLMConfigurationError(
                "messages must contain at least one message",
                request_id=self.request_id,
            )
        for message in self.messages:
            if not isinstance(message, Mapping) or "role" not in message:
                raise LLMConfigurationError(
                    "each message must be a mapping with a role",
                    request_id=self.request_id,
                )
        if self.source is not None and not isinstance(self.source, LLMCallSource):
            raise LLMConfigurationError(
                "source must be an LLMCallSource",
                request_id=self.request_id,
            )
        reserved = {"messages", "stream", "model", "extra_body", "extra_headers"}
        conflict = reserved.intersection(self.parameters)
        if conflict:
            raise LLMConfigurationError(
                "parameters contains reserved keys: " + ", ".join(sorted(conflict)),
                request_id=self.request_id,
            )


@dataclass(slots=True, frozen=True)
class StreamTrigger:
    """流式关键词触发条件。"""

    name: str
    keyword: str
    case_sensitive: bool = True
    emit_once: bool = True
    include_keyword: bool = False
    channel: str = "content"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise LLMConfigurationError("trigger name must not be empty")
        if not self.keyword:
            raise LLMConfigurationError("trigger keyword must not be empty")
        if self.channel not in {"content", "reasoning"}:
            raise LLMConfigurationError(
                "trigger channel must be 'content' or 'reasoning'"
            )


@dataclass(slots=True, frozen=True)
class StreamTriggerResult:
    """一次关键词命中的结果（含触发时间与部分内容）。"""

    request_id: str
    trigger_name: str
    keyword: str
    channel: str
    segment: str
    accumulated_content: str
    keyword_start: int
    keyword_end: int
    occurrence: int
    triggered_at: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trigger_name": self.trigger_name,
            "keyword": self.keyword,
            "channel": self.channel,
            "segment": self.segment,
            "accumulated_content": self.accumulated_content,
            "keyword_start": self.keyword_start,
            "keyword_end": self.keyword_end,
            "occurrence": self.occurrence,
            "triggered_at": self.triggered_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class LLMResponse:
    """标准化 LLM 响应（非流式与流式共用）。"""

    request_id: str
    mode: GenerationMode
    status: StreamStatus
    content: str
    reasoning_content: str = ""
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    stop_reason: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    keyword_events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.completed_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "mode": self.mode.value if isinstance(self.mode, GenerationMode) else str(self.mode),
            "status": self.status.value if isinstance(self.status, StreamStatus) else str(self.status),
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "keyword_events": [dict(event) for event in self.keyword_events],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True, frozen=True)
class StreamEvent:
    """流式调用事件。"""

    type: StreamEventType
    request_id: str
    created_at: float
    delta: str = ""
    channel: str | None = None
    trigger: StreamTriggerResult | None = None
    response: LLMResponse | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "delta": self.delta,
            "channel": self.channel,
            "trigger": self.trigger.to_dict() if self.trigger else None,
            "response": self.response.to_dict() if self.response else None,
            "error": self.error,
        }


@dataclass
class CallRecord:
    """一次调用的完整记录（统计与保存用）。"""

    request_id: str
    mode: str
    status: str
    started_at: float
    completed_at: float | None = None
    elapsed_seconds: float | None = None
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    reasoning_content: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    keyword_events: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(
        cls,
        response: LLMResponse,
        *,
        messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> "CallRecord":
        return cls(
            request_id=response.request_id,
            mode=(
                response.mode.value
                if isinstance(response.mode, GenerationMode)
                else str(response.mode)
            ),
            status=(
                response.status.value
                if isinstance(response.status, StreamStatus)
                else str(response.status)
            ),
            started_at=response.started_at,
            completed_at=response.completed_at or None,
            elapsed_seconds=response.elapsed_seconds if response.completed_at else None,
            model=response.model,
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
            content=response.content,
            reasoning_content=response.reasoning_content,
            messages=[dict(message) for message in messages] if messages else [],
            keyword_events=[dict(event) for event in response.keyword_events],
            stop_reason=response.stop_reason,
            error=response.error,
            metadata=dict(response.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage),
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "messages": [dict(message) for message in self.messages],
            "keyword_events": [dict(event) for event in self.keyword_events],
            "stop_reason": self.stop_reason,
            "error": self.error,
            "metadata": dict(self.metadata),
        }
