"""定时任务系统消息广播与团队记忆写入。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.memory import TeamMemory
from src.runtime import LoopWorker

from .manager import EvaluatorContext


EventSink = Callable[[str], None] | Callable[..., None]


@dataclass(frozen=True)
class SystemMessagePublishResult:
    text: str
    wrote_memory: bool
    broadcasted: bool


class SystemMessageWorker(LoopWorker):
    """周期调用任务 manager 的 system_message，并广播/写入聊天记忆。"""

    def __init__(
        self,
        *,
        manager: Any,
        context_provider: Callable[[], EvaluatorContext | None],
        memory: TeamMemory,
        interval: float,
        broadcast: Callable[[str], None] | None = None,
        event_sink: Callable[..., None] | None = None,
        sender: str = "system",
        name: str = "system-message-worker",
    ) -> None:
        interval = _resolve_interval(manager, interval)
        if interval <= 0:
            raise ValueError("interval must be positive")
        super().__init__(name=name, loop_interval=interval)
        self.manager = manager
        self.context_provider = context_provider
        self.memory = memory
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.sender = sender
        self.published_count = 0

    def iterate(self) -> None:
        try:
            context = self.context_provider()
        except Exception as exc:  # noqa: BLE001
            self._emit("system_message.failed", stage="context", error=str(exc))
            return
        if context is None:
            self._emit("system_message.skipped", reason="missing_context")
            return

        method = getattr(self.manager, "system_message", None)
        if not callable(method):
            self._emit("system_message.skipped", reason="missing_method")
            return

        try:
            value = method(context)
        except Exception as exc:  # noqa: BLE001
            self._emit("system_message.failed", stage="read", error=str(exc))
            return

        text = "" if value is None else str(value).strip()
        if not text:
            return

        result = publish_system_message(
            text,
            memory=self.memory,
            broadcast=self.broadcast,
            event_sink=self._emit,
            sender=self.sender,
            metadata={"source": "task_manager.system_message"},
        )

        if result.wrote_memory or result.broadcasted:
            self.published_count += 1
            self._emit(
                "system_message.published",
                wrote_memory=result.wrote_memory,
                broadcasted=result.broadcasted,
            )

    def _emit(self, event_type: str, **data: Any) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, **data)
        except Exception:  # noqa: BLE001
            return


def publish_system_message(
    text: Any,
    *,
    memory: TeamMemory | None,
    broadcast: Callable[[str], None] | None = None,
    event_sink: Callable[..., None] | None = None,
    sender: str = "system",
    metadata: dict[str, Any] | None = None,
) -> SystemMessagePublishResult:
    message = "" if text is None else str(text).strip()
    if not message:
        return SystemMessagePublishResult("", False, False)

    wrote_memory = False
    broadcasted = False
    if memory is not None:
        try:
            memory.append_history_event(
                agent_id=sender,
                source="system",
                event_type="system",
                content={"message": message},
                metadata=metadata or {"source": "task_manager.system_message"},
            )
            wrote_memory = True
        except Exception as exc:  # noqa: BLE001
            _emit(event_sink, "system_message.failed", stage="memory", error=str(exc))

    if broadcast is not None:
        try:
            broadcast(message)
            broadcasted = True
        except Exception as exc:  # noqa: BLE001
            _emit(event_sink, "system_message.failed", stage="broadcast", error=str(exc))

    return SystemMessagePublishResult(message, wrote_memory, broadcasted)


def _emit(
    event_sink: Callable[..., None] | None,
    event_type: str,
    **data: Any,
) -> None:
    if event_sink is None:
        return
    try:
        event_sink(event_type, **data)
    except Exception:  # noqa: BLE001
        return


def _resolve_interval(manager: Any, default: float) -> float:
    manager_interval = getattr(manager, "system_message_interval_seconds", None)
    if manager_interval is None:
        return default
    if callable(manager_interval):
        manager_interval = manager_interval()
    value = float(manager_interval)
    if value <= 0:
        raise ValueError("system_message_interval_seconds must be positive")
    return min(default, value)


__all__ = [
    "SystemMessagePublishResult",
    "SystemMessageWorker",
    "publish_system_message",
]
