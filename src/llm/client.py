"""LLM 客户端：asyncio 核心（acomplete/astart_stream）+ 同步薄封装。"""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import suppress
from dataclasses import replace
import queue
import re
import threading
import time
from typing import Any, Callable, Iterator, Sequence

from src.recording import (
    EpisodeRecorder,
    LLMCallHandle,
    LLMCallRecorder,
    LLMCallSource,
    RecordingConfig,
    RecordingError,
    RecordingManager,
    RunRecorder,
)

from .config import LLMConfig
from .schemas import (
    GenerationMode,
    LLMConfigurationError,
    LLMRequest,
    LLMRequestCancelled,
    LLMRequestError,
    LLMResponse,
    LLMTimeoutError,
    StreamEvent,
    StreamEventType,
    StreamStatus,
    StreamTrigger,
)
from .streaming import StreamingSession

_STREAM_TERMINAL_EVENTS = {
    StreamEventType.COMPLETED,
    StreamEventType.STOPPED,
    StreamEventType.FAILED,
}


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


class LLMClient:
    """OpenAI 兼容 chat completions 客户端。

    - async 核心：acomplete / astart_stream
    - 同步薄封装：complete / stream（内部维护每实例后台事件循环线程）
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: Any | None = None,
        call_recorder: LLMCallRecorder | None = None,
        recording_config: RecordingConfig | None = None,
    ):
        self.config = config
        self._client = client if client is not None else self._create_client()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._closed = False
        self._active_requests: dict[Any, Callable[[str], None]] = {}
        self._active_requests_lock = threading.Lock()
        self._call_recorder = call_recorder
        self._recording_config = recording_config
        self._recording_lock = threading.RLock()
        self._recording_started = False
        self._owned_recording_run: RunRecorder | None = None
        self._owned_recording_episode: EpisodeRecorder | None = None

    @classmethod
    def from_env(
        cls,
        env_path: str | None = None,
        *,
        client: Any | None = None,
        call_recorder: LLMCallRecorder | None = None,
        recording_config: RecordingConfig | None = None,
    ) -> "LLMClient":
        return cls(
            LLMConfig.from_env(env_path),
            client=client,
            call_recorder=call_recorder,
            recording_config=recording_config,
        )

    @property
    def call_recorder(self) -> LLMCallRecorder | None:
        return self._call_recorder

    def bind_call_recorder(self, recorder: LLMCallRecorder) -> None:
        """Bind an externally owned recorder before the first model request."""
        with self._recording_lock:
            if self._call_recorder is recorder:
                return
            if self._recording_started or self._call_recorder is not None:
                raise RecordingError(
                    "LLM call recorder cannot be replaced after recording is bound"
                )
            self._call_recorder = recorder

    def _ensure_call_recorder(self) -> LLMCallRecorder:
        with self._recording_lock:
            if self._closed:
                raise RuntimeError("LLMClient has been closed")
            if self._call_recorder is None:
                config = self._recording_config or RecordingConfig()
                if self.config.api_key and self.config.api_key not in config.redact_values:
                    config = replace(
                        config,
                        redact_values=(*config.redact_values, self.config.api_key),
                    )
                manager = RecordingManager(config)
                run = manager.start_run(
                    "standalone",
                    metadata={"llm_config": self.config.safe_dict()},
                )
                episode = run.start_episode(metadata={"kind": "standalone_llm"})
                self._owned_recording_run = run
                self._owned_recording_episode = episode
                self._call_recorder = episode.llm_calls
            self._recording_started = True
            return self._call_recorder

    def _record_request(
        self,
        request: LLMRequest,
        *,
        mode: str,
        started_at: float,
    ) -> LLMCallHandle:
        recorder = self._ensure_call_recorder()
        source = request.source or LLMCallSource(component="standalone")
        request_payload = {
            "requested_model": request.model,
            "resolved_model": request.model or self.config.model,
            "messages": [dict(message) for message in request.messages],
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self.config.temperature
            ),
            "max_tokens": (
                request.max_tokens
                if request.max_tokens is not None
                else self.config.max_tokens
            ),
            "extra_body": {**self.config.extra_body, **request.extra_body},
            "parameters": dict(request.parameters),
            "extra_header_names": sorted(
                set(self.config.extra_headers).union(request.extra_headers)
            ),
            "stream": mode == GenerationMode.STREAM.value,
            "metadata": dict(request.metadata),
        }
        return recorder.begin(
            request_id=request.request_id,
            mode=mode,
            source=source,
            request=request_payload,
            started_at=started_at,
        )

    @staticmethod
    def _response_payload(response: LLMResponse) -> dict[str, Any]:
        return {
            "request_id": response.request_id,
            "mode": response.mode.value,
            "status": response.status.value,
            "content": response.content,
            "reasoning_content": response.reasoning_content,
            "model": response.model,
            "finish_reason": response.finish_reason,
            "stop_reason": response.stop_reason,
            "keyword_events": [dict(event) for event in response.keyword_events],
            "metadata": dict(response.metadata),
        }

    def _create_client(self) -> Any:
        try:
            from openai import AsyncOpenAI
        except Exception as exc:
            raise LLMConfigurationError(
                "openai 包未安装, 请安装 openai>=2.45,<3"
            ) from exc
        return AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.api_url,
            timeout=self.config.timeout,
            max_retries=self.config.max_retries,
            default_headers=self.config.extra_headers or None,
        )

    def _redact(self, value: Any) -> str:
        text = str(value)
        if self.config.api_key:
            text = text.replace(self.config.api_key, "***")
        return re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer ***", text)

    def _build_params(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": [dict(message) for message in request.messages],
            "stream": stream,
        }
        temperature = (
            request.temperature
            if request.temperature is not None
            else self.config.temperature
        )
        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else self.config.max_tokens
        )
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens

        extra_body = dict(self.config.extra_body)
        extra_body.update(request.extra_body)
        if not self.config.thinking_enabled:
            if "reasoning_effort" in request.parameters:
                raise LLMConfigurationError(
                    "reasoning_effort cannot be used while thinking is disabled",
                    request_id=request.request_id,
                )
            if self.config.thinking_disabled_body:
                extra_body.update(self.config.thinking_disabled_body)
            elif self.config.disable_thinking_param:
                extra_body[self.config.disable_thinking_param] = False
        if extra_body:
            params["extra_body"] = extra_body

        extra_headers = dict(self.config.extra_headers)
        extra_headers.update(request.extra_headers)
        if extra_headers:
            params["extra_headers"] = extra_headers

        if stream and self.config.stream_include_usage:
            params["stream_options"] = {"include_usage": True}
        params.update(request.parameters)
        return params

    # ------------------------------------------------------------------
    # async 核心
    # ------------------------------------------------------------------

    async def acomplete(
        self,
        request: LLMRequest,
        *,
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> LLMResponse:
        """非流式调用：记录开始/结束时间与 token 消耗。"""
        started_at = time.time()
        handle = self._record_request(
            request,
            mode=GenerationMode.COMPLETE.value,
            started_at=started_at,
        )
        try:
            params = self._build_params(request, stream=False)
            response = await self._await_with_stop(
                self._client.chat.completions.create(**params),
                stop_event,
            )
            raw = _dump_object(response)
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise LLMRequestError(
                    "Model response contains no choices", request_id=request.request_id
                )
            choice = choices[0]
            message = getattr(choice, "message", None)
            message_data = _dump_object(message)
            content = _text_value(
                getattr(message, "content", None) if message is not None else None
            )
            reasoning = _text_value(
                getattr(message, "reasoning_content", None)
                if message is not None
                else None
            ) or _text_value(message_data.get("reasoning_content"))
            if (
                reasoning
                and not self.config.thinking_enabled
                and self.config.reject_reasoning_when_disabled
            ):
                raise LLMRequestError(
                    "Provider returned reasoning_content while thinking is disabled",
                    request_id=request.request_id,
                )
            usage = _dump_object(getattr(response, "usage", None))
            result = LLMResponse(
                request_id=request.request_id,
                mode=GenerationMode.COMPLETE,
                status=StreamStatus.COMPLETED,
                content=content,
                reasoning_content=reasoning,
                model=getattr(response, "model", None),
                finish_reason=getattr(choice, "finish_reason", None),
                usage=usage,
                raw=raw,
                started_at=started_at,
                completed_at=time.time(),
                metadata=dict(request.metadata),
            )
        except asyncio.CancelledError:
            handle.finish(
                status="cancelled",
                error_type="CancelledError",
                error="LLM request was cancelled",
            )
            raise
        except TimeoutError as exc:
            wrapped = LLMTimeoutError(self._redact(exc), request_id=request.request_id)
            handle.finish(
                status="timeout",
                error_type=type(wrapped).__name__,
                error=str(wrapped),
            )
            raise wrapped from exc
        except RecordingError:
            raise
        except (LLMConfigurationError, LLMRequestError) as exc:
            handle.finish(
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        except Exception as exc:
            wrapped = LLMRequestError(
                self._redact(exc), request_id=request.request_id
            )
            handle.finish(
                status="failed",
                error_type=type(wrapped).__name__,
                error=str(wrapped),
            )
            raise wrapped from exc

        handle.finish(
            status="completed",
            response=self._response_payload(result),
            usage=result.usage,
            completed_at=result.completed_at,
        )
        return result

    async def astart_stream(
        self,
        request: LLMRequest,
        *,
        triggers: Sequence[StreamTrigger] = (),
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> StreamingSession:
        """启动后台流式会话并立即返回。"""
        started_at = time.time()
        handle = self._record_request(
            request,
            mode=GenerationMode.STREAM.value,
            started_at=started_at,
        )
        try:
            session = StreamingSession(
                model=self,
                request=request,
                triggers=triggers,
                external_stop_event=stop_event,
                recording_handle=handle,
                started_at=started_at,
            )
            session.start()
            return session
        except Exception as exc:
            if isinstance(exc, RecordingError):
                raise
            handle.finish(
                status="failed",
                error_type=type(exc).__name__,
                error=self._redact(exc),
            )
            raise

    # ------------------------------------------------------------------
    # 同步薄封装
    # ------------------------------------------------------------------

    def complete(
        self,
        request: LLMRequest,
        *,
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> LLMResponse:
        """同步非流式调用（内部事件循环线程）。"""
        return self._run_sync(
            self.acomplete(request, stop_event=stop_event)
        )

    def stream(
        self,
        request: LLMRequest,
        *,
        triggers: Sequence[StreamTrigger] = (),
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> Iterator[StreamEvent]:
        """同步流式调用，按事件迭代返回（含 KEYWORD_REACHED 部分返回）。"""
        loop = self._get_loop()
        out_queue: queue.Queue[StreamEvent] = queue.Queue()

        state: dict[str, Any] = {
            "session": None,
            "task": None,
            "stop_enqueued": False,
        }

        def enqueue_stopped() -> None:
            if state["stop_enqueued"]:
                return
            state["stop_enqueued"] = True
            out_queue.put(
                StreamEvent(
                    type=StreamEventType.STOPPED,
                    request_id=request.request_id,
                    created_at=time.time(),
                    error="LLM stream cancelled",
                )
            )

        async def _consume() -> None:
            session = await self.astart_stream(
                request, triggers=triggers, stop_event=stop_event
            )
            state["session"] = session
            try:
                async for event in session.events():
                    out_queue.put(event)
                    if event.type in _STREAM_TERMINAL_EVENTS:
                        return
            except asyncio.CancelledError:
                with suppress(Exception):
                    await session.stop(reason="client_cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                out_queue.put(
                    StreamEvent(
                        type=StreamEventType.FAILED,
                        request_id=request.request_id,
                        created_at=time.time(),
                        error=self._redact(str(exc)),
                    )
                )

        task = asyncio.run_coroutine_threadsafe(_consume(), loop)
        state["task"] = task

        def request_stop(reason: str) -> None:
            if task.done() or loop.is_closed():
                return

            async def stop_session() -> None:
                session = state["session"]
                if session is not None:
                    try:
                        await session.stop(reason=reason)
                    except Exception:  # noqa: BLE001
                        enqueue_stopped()
                    return
                task.cancel()
                enqueue_stopped()

            asyncio.run_coroutine_threadsafe(stop_session(), loop)

        self._track_request(task, request_stop)

        def _iterator() -> Iterator[StreamEvent]:
            try:
                while True:
                    event = out_queue.get()
                    yield event
                    if event.type in _STREAM_TERMINAL_EVENTS:
                        return
            finally:
                request_stop("iterator_closed")

        return _iterator()

    # ------------------------------------------------------------------
    # 事件循环线程管理
    # ------------------------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._closed:
                raise RuntimeError("LLMClient has been closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    name="llm-client-loop",
                    daemon=True,
                )
                self._loop = loop
                self._loop_thread = thread
                thread.start()
            return self._loop

    def _run_sync(self, coro: Any, timeout: float | None = None) -> Any:
        loop = self._get_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        self._track_request(
            future,
            lambda _reason: future.cancel(),
        )
        try:
            return future.result(timeout=timeout)
        except (FutureCancelledError, asyncio.CancelledError) as exc:
            raise LLMRequestCancelled("LLM request cancelled") from exc

    def _track_request(
        self,
        future: Any,
        cancel: Callable[[str], None],
    ) -> None:
        with self._active_requests_lock:
            self._active_requests[future] = cancel
        future.add_done_callback(self._untrack_request)

    def _untrack_request(self, future: Any) -> None:
        with self._active_requests_lock:
            self._active_requests.pop(future, None)

    def cancel_active_requests(self, reason: str = "caller_stop") -> int:
        """取消当前客户端上的请求，并主动结束正在消费的流。"""
        with self._active_requests_lock:
            cancellers = tuple(self._active_requests.values())
        for cancel in cancellers:
            try:
                cancel(reason)
            except Exception:  # noqa: BLE001
                continue
        return len(cancellers)

    @staticmethod
    async def _wait_for_external_stop(event: asyncio.Event | threading.Event) -> None:
        if isinstance(event, asyncio.Event):
            await event.wait()
            return
        while not event.is_set():
            await asyncio.sleep(0.05)

    async def _await_with_stop(
        self,
        awaitable: Any,
        stop_event: asyncio.Event | threading.Event | None,
    ) -> Any:
        if stop_event is None:
            return await awaitable
        request_task = asyncio.ensure_future(awaitable)
        stop_task = asyncio.create_task(self._wait_for_external_stop(stop_event))
        try:
            done, _ = await asyncio.wait(
                (request_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if request_task in done:
                return request_task.result()
            request_task.cancel()
            with suppress(asyncio.CancelledError):
                await request_task
            raise asyncio.CancelledError
        finally:
            if not stop_task.done():
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def close(self) -> None:
        """停止后台事件循环线程（幂等）。"""
        with self._loop_lock:
            if self._closed:
                return
            loop, thread = self._loop, self._loop_thread
            self._loop = None
            self._loop_thread = None
            self._closed = True
        self.cancel_active_requests(reason="client_closed")
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
        episode = self._owned_recording_episode
        run = self._owned_recording_run
        if episode is not None:
            summary = episode.llm_calls.summary()
            failed = any(
                summary["status_counts"].get(status, 0)
                for status in ("failed", "timeout", "cancelled")
            ) or bool(summary["incomplete_calls"])
            episode.finalize(status="failed" if failed else "completed")
        if run is not None:
            run.finalize()


class AgentLLMClientPool:
    """Route model requests to isolated clients per agent and call channel.

    A channel is identified by the request's agent, component, and operation.
    Repeated requests on one channel reuse its client, while independent
    planning roles get separate event loops and HTTP connection pools.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        call_recorder: LLMCallRecorder | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._call_recorder = call_recorder
        self._client_factory = client_factory or LLMClient
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def call_recorder(self) -> LLMCallRecorder | None:
        return self._call_recorder

    def bind_call_recorder(self, recorder: LLMCallRecorder) -> None:
        """Bind an episode recorder before the first routed request."""
        with self._lock:
            if self._call_recorder is recorder:
                return
            if self._clients:
                raise RecordingError(
                    "LLM client pool cannot replace recorder after a client is created"
                )
            self._call_recorder = recorder

    def complete(
        self,
        request: LLMRequest,
        *,
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> LLMResponse:
        client = self._client_for(request)
        if stop_event is None:
            return client.complete(request)
        return client.complete(request, stop_event=stop_event)

    async def acomplete(
        self,
        request: LLMRequest,
        *,
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> LLMResponse:
        """异步非流式调用，沿用与同步调用相同的隔离路由。"""
        client = self._client_for(request)
        if stop_event is None:
            return await client.acomplete(request)
        return await client.acomplete(request, stop_event=stop_event)

    def stream(
        self,
        request: LLMRequest,
        *,
        triggers: Sequence[StreamTrigger] = (),
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> Iterator[StreamEvent]:
        return self._client_for(request).stream(
            request,
            triggers=triggers,
            stop_event=stop_event,
        )

    async def astart_stream(
        self,
        request: LLMRequest,
        *,
        triggers: Sequence[StreamTrigger] = (),
        stop_event: asyncio.Event | threading.Event | None = None,
    ) -> StreamingSession:
        """异步启动流式调用，沿用与同步调用相同的隔离路由。"""
        return await self._client_for(request).astart_stream(
            request,
            triggers=triggers,
            stop_event=stop_event,
        )

    def cancel_active_requests(self, reason: str = "caller_stop") -> int:
        """取消所有已创建客户端上的活动请求。"""
        with self._lock:
            clients = tuple(self._clients.values())
        cancelled = 0
        for client in clients:
            cancel = getattr(client, "cancel_active_requests", None)
            if not callable(cancel):
                continue
            try:
                result = cancel(reason=reason)
            except TypeError:
                result = cancel()
            cancelled += result if isinstance(result, int) else 1
        return cancelled

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _client_for(self, request: LLMRequest) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("AgentLLMClientPool has been closed")
            key = self._channel_key(request)
            client = self._clients.get(key)
            if client is None:
                client = self._client_factory(
                    self.config,
                    call_recorder=self._call_recorder,
                )
                self._clients[key] = client
            return client

    @staticmethod
    def _channel_key(request: LLMRequest) -> tuple[str, str, str]:
        metadata = request.metadata or {}
        source = request.source
        agent_id = metadata.get("agent_id") or (
            source.agent_id if source is not None else None
        )
        component = metadata.get("component") or (
            source.component if source is not None else None
        )
        operation = metadata.get("operation") or (
            source.operation if source is not None else None
        )
        return (
            str(agent_id or "shared"),
            str(component or "default"),
            str(operation or "default"),
        )
