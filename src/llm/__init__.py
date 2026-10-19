"""LLM 调用模块：非流式/流式模型调用、统计与保存。

设计参照 D:\\MC\\MCAgent\\framework\\arc\\models.py，保持自包含、不依赖 MCAgent。
"""

from .client import AgentLLMClientPool, LLMClient
from .config import LLMConfig
from .save import save_call
from src.recording import LLMCallSource
from .schemas import (
    CallRecord,
    GenerationMode,
    LLMConfigurationError,
    LLMError,
    LLMRequest,
    LLMRequestCancelled,
    LLMRequestError,
    LLMResponse,
    LLMStreamError,
    LLMTimeoutError,
    StreamEvent,
    StreamEventType,
    StreamStatus,
    StreamTrigger,
    StreamTriggerResult,
    TriggerNotFoundError,
)

__all__ = [
    "CallRecord",
    "AgentLLMClientPool",
    "GenerationMode",
    "LLMClient",
    "LLMCallSource",
    "LLMConfig",
    "LLMConfigurationError",
    "LLMError",
    "LLMRequest",
    "LLMRequestCancelled",
    "LLMRequestError",
    "LLMResponse",
    "LLMStreamError",
    "LLMTimeoutError",
    "StreamEvent",
    "StreamEventType",
    "StreamStatus",
    "StreamTrigger",
    "StreamTriggerResult",
    "TriggerNotFoundError",
    "save_call",
]
