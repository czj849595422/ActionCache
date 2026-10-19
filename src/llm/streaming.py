"""流式会话：后台消费模型流，支持关键词触发、部分返回、停止与统计。"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from .schemas import (
    GenerationMode,
    LLMConfigurationError,
    LLMRequest,
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


def _dump_object(value: Any) -> dict[str, Any]:
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return {}
    try:
        result = dump(mode="json")
    except TypeError:
        result = dump()
    return result if isinstance(result, dict) else {}


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


@dataclass(slots=True)
class _TriggerState:
    trigger: StreamTrigger
    order: int
    search_from: int = 0
    emitted_count: int = 0
    pattern: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        flags = 0 if self.trigger.case_sensitive else re.IGNORECASE
        self.pattern = re.compile(re.escape(self.trigger.keyword), flags)


class StreamingSession:
    """后台消费的模型流会话，带关键词通知、部分返回与停止能力。"""

    _TERMINAL_EVENTS = {
        StreamEventType.COMPLETED,
        StreamEventType.STOPPED,
        StreamEventType.FAILED,
    }

    def __init__(
        self,
        *,
        model: Any,
        request: LLMRequest,
        triggers: Sequence[StreamTrigger],
        external_stop_event: asyncio.Event | threading.Event | None,
        recording_handle: Any,
        started_at: float,
    ):
        names = [trigger.name for trigger in triggers]
        if len(names) != len(set(names)):
            raise LLMConfigurationError(
                "stream trigger names must be unique",
                request_id=request.request_id,
            )

        self._model = model
        self.request = request
        self._trigger_states = [
            _TriggerState(trigger=trigger, order=index)
            for index, trigger in enumerate(triggers)
        ]
        self._external_stop_event = external_stop_event
        self._recording_handle = recording_handle
        self._internal_stop_event = asyncio.Event()
        self._event_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._finished_event = asyncio.Event()
        self._trigger_events = {name: asyncio.Event() for name in names}
        self._trigger_results: dict[str, list[StreamTriggerResult]] = {
            name: [] for name in names
        }
        self._segment_offsets = {"content": 0, "reasoning": 0}
        self._content = ""
        self._reasoning_content = ""
        self._raw_chunks: list[dict[str, Any]] = []
        self._usage: dict[str, Any] = {}
        self._model_name: str | None = None
        self._finish_reason: str | None = None
        self._response: LLMResponse | None = None
        self._stream: Any | None = None
        self._stream_closed = False
        self._stop_reason: str | None = None
        self._task: asyncio.Task[None] | None = None
        self.started_at = started_at
        self.status = StreamStatus.RUNNING

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("streaming session has already started")
        self._task = asyncio.create_task(
            self._run(), name=f"llm-stream-{self.request.request_id}"
        )

    @property
    def content(self) -> str:
        return self._content

    @property
    def reasoning_content(self) -> str:
        return self._reasoning_content

    @property
    def response(self) -> LLMResponse | None:
        return self._response

    async def events(self) -> AsyncIterator[StreamEvent]:
        """按顺序产出事件，终端事件（completed/stopped/failed）后结束。"""
        while True:
            event = await self._event_queue.get()
            yield event
            if event.type in self._TERMINAL_EVENTS:
                return

    async def wait_for_trigger(
        self,
        name: str,
        *,
        occurrence: int = 1,
        timeout: float | None = None,
    ) -> StreamTriggerResult:
        """等待指定关键词的第 occurrence 次命中（可多次返回）。"""
        if name not in self._trigger_events:
            raise LLMConfigurationError(
                f"Unknown stream trigger: {name}",
                request_id=self.request.request_id,
            )
        if occurrence < 1:
            raise LLMConfigurationError(
                "occurrence must be at least 1",
                request_id=self.request.request_id,
            )

        async def wait() -> StreamTriggerResult:
            event = self._trigger_events[name]
            while len(self._trigger_results[name]) < occurrence:
                if self._finished_event.is_set():
                    raise TriggerNotFoundError(
                        f"Trigger {name!r} occurrence {occurrence} was not reached",
                        request_id=self.request.request_id,
                    )
                event.clear()
                await event.wait()
            return self._trigger_results[name][occurrence - 1]

        try:
            return await asyncio.wait_for(wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                f"Timed out waiting for trigger {name!r}",
                request_id=self.request.request_id,
            ) from exc

    async def wait_finished(self, *, timeout: float | None = None) -> LLMResponse:
        try:
            await asyncio.wait_for(self._finished_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                "Timed out waiting for stream completion",
                request_id=self.request.request_id,
            ) from exc
        assert self._response is not None
        return self._response

    async def stop(self, reason: str = "caller_stop") -> LLMResponse:
        """请求停止后台流（幂等），返回最终响应。"""
        if not self._finished_event.is_set():
            if self._stop_reason is None:
                self._stop_reason = reason
            self._internal_stop_event.set()
        return await self.wait_finished()

    async def _publish(self, event: StreamEvent) -> None:
        self._event_queue.put_nowait(event)

    async def _wait_for_stop(self) -> None:
        tasks = [asyncio.create_task(self._internal_stop_event.wait())]
        if self._external_stop_event is not None:
            tasks.append(
                asyncio.create_task(self._wait_for_external_stop())
            )
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            if (
                self._external_stop_event is not None
                and self._external_stop_event.is_set()
                and self._stop_reason is None
            ):
                self._stop_reason = "external_stop"
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _wait_for_external_stop(self) -> None:
        event = self._external_stop_event
        if event is None:
            return
        if isinstance(event, asyncio.Event):
            await event.wait()
            return
        while not event.is_set():
            await asyncio.sleep(0.05)

    async def _run(self) -> None:
        await self._publish(
            StreamEvent(
                type=StreamEventType.STARTED,
                request_id=self.request.request_id,
                created_at=time.time(),
            )
        )
        stop_task = asyncio.create_task(self._wait_for_stop())
        create_task: asyncio.Task[Any] | None = None
        next_task: asyncio.Task[Any] | None = None
        try:
            params = self._model._build_params(self.request, stream=True)
            create_task = asyncio.create_task(
                self._model._client.chat.completions.create(**params)
            )
            done, _ = await asyncio.wait(
                (create_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                create_task.cancel()
                with suppress(asyncio.CancelledError):
                    await create_task
                await self._finalize_stopped()
                return

            self._stream = create_task.result()
            iterator = self._stream.__aiter__()
            while True:
                next_task = asyncio.create_task(iterator.__anext__())
                done, _ = await asyncio.wait(
                    (next_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    next_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await next_task
                    await self._close_stream()
                    await self._finalize_stopped()
                    return
                try:
                    chunk = next_task.result()
                except StopAsyncIteration:
                    await self._finalize_completed()
                    return
                await self._consume_chunk(chunk)
        except asyncio.CancelledError:
            if self._stop_reason is None:
                self._stop_reason = "session_cancelled"
            await self._close_stream()
            await self._finalize_stopped()
        except TimeoutError as exc:
            await self._close_stream()
            await self._finalize_failed(
                self._model._redact(exc),
                record_status="timeout",
                error_type="LLMTimeoutError",
            )
        except Exception as exc:
            await self._close_stream()
            await self._finalize_failed(
                self._model._redact(exc),
                error_type=type(exc).__name__,
            )
        finally:
            if create_task is not None and not create_task.done():
                create_task.cancel()
            if next_task is not None:
                if not next_task.done():
                    next_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await next_task
                else:
                    with suppress(Exception):
                        next_task.exception()
            if not stop_task.done():
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task

    async def _consume_chunk(self, chunk: Any) -> None:
        raw = _dump_object(chunk)
        if self._model.config.retain_raw_chunks:
            self._raw_chunks.append(raw)
        self._model_name = getattr(chunk, "model", None) or self._model_name
        usage = _dump_object(getattr(chunk, "usage", None))
        if usage:
            self._usage = usage

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None:
            self._finish_reason = str(finish_reason)
        delta = getattr(choice, "delta", None)
        delta_data = _dump_object(delta)
        content_delta = _text_value(
            getattr(delta, "content", None) if delta is not None else None
        )
        reasoning_delta = _text_value(
            getattr(delta, "reasoning_content", None)
            if delta is not None
            else None
        ) or _text_value(delta_data.get("reasoning_content"))

        if content_delta:
            self._content += content_delta
            self._check_buffer_size()
            await self._publish(
                StreamEvent(
                    type=StreamEventType.CONTENT_DELTA,
                    request_id=self.request.request_id,
                    created_at=time.time(),
                    delta=content_delta,
                    channel="content",
                )
            )
            await self._match_triggers("content")
        if reasoning_delta:
            self._reasoning_content += reasoning_delta
            self._check_buffer_size()
            if (
                not self._model.config.thinking_enabled
                and self._model.config.reject_reasoning_when_disabled
            ):
                raise LLMStreamError(
                    "Provider returned reasoning_content while thinking is disabled",
                    request_id=self.request.request_id,
                )
            await self._publish(
                StreamEvent(
                    type=StreamEventType.REASONING_DELTA,
                    request_id=self.request.request_id,
                    created_at=time.time(),
                    delta=reasoning_delta,
                    channel="reasoning",
                )
            )
            await self._match_triggers("reasoning")

    def _check_buffer_size(self) -> None:
        if (
            len(self._content) + len(self._reasoning_content)
            > self._model.config.maximum_buffer_characters
        ):
            raise LLMStreamError(
                "Model stream exceeded maximum_buffer_characters",
                request_id=self.request.request_id,
            )

    async def _match_triggers(self, channel: str) -> None:
        text = self._content if channel == "content" else self._reasoning_content
        matches: list[tuple[int, int, int, _TriggerState]] = []
        for state in self._trigger_states:
            trigger = state.trigger
            if trigger.channel != channel:
                continue
            if trigger.emit_once and state.emitted_count:
                continue

            position = state.search_from
            while True:
                match = state.pattern.search(text, position)
                if match is None:
                    safe_tail = max(0, len(text) - len(trigger.keyword) + 1)
                    state.search_from = max(position, safe_tail)
                    break
                matches.append((match.start(), state.order, match.end(), state))
                position = match.end()
                state.search_from = position
                if trigger.emit_once:
                    break

        for start, _, end, state in sorted(
            matches, key=lambda item: (item[0], item[1])
        ):
            trigger = state.trigger
            if trigger.emit_once and state.emitted_count:
                continue
            state.emitted_count += 1
            payload_end = end if trigger.include_keyword else start
            segment_start = self._segment_offsets[channel]
            result = StreamTriggerResult(
                request_id=self.request.request_id,
                trigger_name=trigger.name,
                keyword=trigger.keyword,
                channel=channel,
                segment=text[segment_start:payload_end],
                accumulated_content=text[:payload_end],
                keyword_start=start,
                keyword_end=end,
                occurrence=state.emitted_count,
                triggered_at=time.time(),
                metadata=dict(trigger.metadata),
            )
            self._segment_offsets[channel] = max(
                self._segment_offsets[channel], end
            )
            self._trigger_results[trigger.name].append(result)
            self._trigger_events[trigger.name].set()
            await self._publish(
                StreamEvent(
                    type=StreamEventType.KEYWORD_REACHED,
                    request_id=self.request.request_id,
                    created_at=result.triggered_at,
                    channel=channel,
                    trigger=result,
                )
            )

    async def _close_stream(self) -> None:
        if self._stream is None or self._stream_closed:
            return
        self._stream_closed = True
        close = getattr(self._stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _finalize_completed(self) -> None:
        await self._finalize(StreamStatus.COMPLETED)

    async def _finalize_stopped(self) -> None:
        await self._finalize(
            StreamStatus.STOPPED,
            stop_reason=self._stop_reason or "external_stop",
            record_status=(
                "cancelled" if self._stop_reason == "session_cancelled" else "stopped"
            ),
        )

    async def _finalize_failed(
        self,
        error: str,
        *,
        record_status: str = "failed",
        error_type: str = "LLMStreamError",
    ) -> None:
        await self._finalize(
            StreamStatus.FAILED,
            error=error,
            record_status=record_status,
            error_type=error_type,
        )

    async def _finalize(
        self,
        status: StreamStatus,
        *,
        stop_reason: str | None = None,
        error: str | None = None,
        record_status: str | None = None,
        error_type: str | None = None,
    ) -> None:
        if self._finished_event.is_set():
            return
        self.status = status
        keyword_events = [
            result.to_dict()
            for results in self._trigger_results.values()
            for result in results
        ]
        response = LLMResponse(
            request_id=self.request.request_id,
            mode=GenerationMode.STREAM,
            status=status,
            content=self._content,
            reasoning_content=self._reasoning_content,
            model=self._model_name,
            finish_reason=self._finish_reason,
            usage=dict(self._usage),
            raw=(
                {"chunks": list(self._raw_chunks)}
                if self._model.config.retain_raw_chunks
                else {}
            ),
            started_at=self.started_at,
            completed_at=time.time(),
            stop_reason=stop_reason,
            error=error,
            keyword_events=keyword_events,
            metadata=dict(self.request.metadata),
        )
        terminal_status = record_status or {
            StreamStatus.COMPLETED: "completed",
            StreamStatus.STOPPED: "stopped",
            StreamStatus.FAILED: "failed",
        }[status]
        try:
            self._recording_handle.finish(
                status=terminal_status,
                response=self._model._response_payload(response),
                usage=response.usage,
                error_type=error_type,
                error=error,
                completed_at=response.completed_at,
            )
        except Exception as exc:  # recording failure must not look successful
            status = StreamStatus.FAILED
            error = self._model._redact(exc)
            response = LLMResponse(
                request_id=self.request.request_id,
                mode=GenerationMode.STREAM,
                status=status,
                content=self._content,
                reasoning_content=self._reasoning_content,
                model=self._model_name,
                finish_reason=self._finish_reason,
                usage=dict(self._usage),
                raw={},
                started_at=self.started_at,
                completed_at=time.time(),
                error=error,
                keyword_events=keyword_events,
                metadata=dict(self.request.metadata),
            )
        self._response = response
        event_type = {
            StreamStatus.COMPLETED: StreamEventType.COMPLETED,
            StreamStatus.STOPPED: StreamEventType.STOPPED,
            StreamStatus.FAILED: StreamEventType.FAILED,
        }[status]
        await self._publish(
            StreamEvent(
                type=event_type,
                request_id=self.request.request_id,
                created_at=response.completed_at,
                response=response,
                error=error,
            )
        )
        self._finished_event.set()
        for event in self._trigger_events.values():
            event.set()
