#!/usr/bin/env python3
"""Asynchronous, batched public-chat delivery for a running server."""

from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from itertools import count
from queue import Empty, Full, Queue
from typing import Any, Callable

from .mcrcon import PersistentRcon


EventSink = Callable[..., None]


@dataclass(frozen=True)
class _BroadcastMessage:
    message_id: int
    text: str
    queued_at: float
    queued_wall_time: float


class AsyncBroadcastDispatcher:
    """Queue public messages and flush them through an independent RCON path.

    The caller only performs an in-memory queue operation.  The dispatcher
    combines messages into one ``tellraw`` command so the public-chat path
    does not hold up planning or action workers.
    """

    def __init__(
        self,
        server: Any,
        *,
        event_sink: EventSink | None = None,
        name: str = "mc-broadcast",
        batch_window_seconds: float = 0.2,
        max_batch_messages: int = 8,
        max_command_bytes: int = 3072,
        max_queue_size: int = 1024,
        response_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if batch_window_seconds < 0:
            raise ValueError("batch_window_seconds must not be negative")
        if max_batch_messages < 1:
            raise ValueError("max_batch_messages must be positive")
        if max_command_bytes < 256:
            raise ValueError("max_command_bytes must be at least 256")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        if response_timeout <= 0:
            raise ValueError("response_timeout must be positive")

        self._server = server
        self._event_sink = event_sink
        self._name = name
        self._batch_window_seconds = float(batch_window_seconds)
        self._max_batch_messages = int(max_batch_messages)
        self._max_command_bytes = int(max_command_bytes)
        self._response_timeout = float(response_timeout)
        self._clock = clock
        self._wall_clock = wall_clock
        self._queue: Queue[_BroadcastMessage] = Queue(maxsize=max_queue_size)
        self._sequence = count(1)
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._rcon: PersistentRcon | None = None

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("broadcast dispatcher is closed")
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, message: str) -> bool:
        text = str(message).strip()
        if not text:
            return False
        with self._state_lock:
            if self._closed:
                self._emit(
                    "broadcast_failed",
                    reason="dispatcher_closed",
                    message_count=1,
                )
                return False
            if not self._started:
                self.start()
            item = _BroadcastMessage(
                message_id=next(self._sequence),
                text=text,
                queued_at=self._clock(),
                queued_wall_time=self._wall_clock(),
            )
            try:
                self._queue.put_nowait(item)
            except Full:
                self._emit(
                    "broadcast_failed",
                    reason="queue_full",
                    message_count=1,
                )
                return False
            self._emit(
                "broadcast_queued",
                message_id=item.message_id,
                message_length=len(text),
                queue_size=self._queue.qsize(),
                queued_at=self._wall_clock(),
            )
            return True

    def close(self, timeout: float = 2.0) -> None:
        """Stop after flushing queued messages for at most ``timeout`` seconds."""
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=float(timeout))
        if thread is not None and thread.is_alive():
            dropped = self._drain_unprocessed()
            if dropped:
                self._emit(
                    "broadcast_failed",
                    reason="shutdown_timeout",
                    message_count=len(dropped),
                    message_ids=[item.message_id for item in dropped],
                )
            return
        self._close_rcon()

    def _run(self) -> None:
        pending: _BroadcastMessage | None = None
        try:
            while True:
                if pending is None:
                    try:
                        pending = self._queue.get(timeout=0.1)
                    except Empty:
                        if self._stop_event.is_set():
                            break
                        continue

                batch, pending = self._collect_batch(pending)
                self._send_batch(batch)
                for _ in batch:
                    self._queue.task_done()
                if self._stop_event.is_set() and self._queue.empty():
                    break
        finally:
            if pending is not None:
                self._queue.task_done()
            self._close_rcon()

    def _collect_batch(
        self, first: _BroadcastMessage
    ) -> tuple[list[_BroadcastMessage], _BroadcastMessage | None]:
        batch = [first]
        deadline = self._clock() + self._batch_window_seconds
        while len(batch) < self._max_batch_messages:
            if self._stop_event.is_set():
                timeout = 0.0
            else:
                timeout = max(0.0, deadline - self._clock())
                if timeout == 0.0:
                    break
            try:
                candidate = self._queue.get(timeout=timeout)
            except Empty:
                break
            if self._command_size(batch + [candidate]) <= self._max_command_bytes:
                batch.append(candidate)
                continue
            return batch, candidate
        return batch, None

    def _send_batch(self, batch: list[_BroadcastMessage]) -> None:
        command = self._build_command(batch)
        try:
            self._send_command(command)
        except Exception as exc:  # noqa: BLE001
            self._close_rcon()
            self._emit(
                "broadcast_failed",
                reason="send_error",
                error=str(exc),
                message_count=len(batch),
                message_ids=[item.message_id for item in batch],
            )
            return
        sent_at = self._wall_clock()
        self._emit(
            "broadcast_sent",
            message_count=len(batch),
            message_ids=[item.message_id for item in batch],
            command_bytes=len(command.encode("utf-8")),
            queued_at=min(item.queued_wall_time for item in batch),
            sent_at=sent_at,
            queue_delay_seconds=max(
                0.0,
                self._clock() - min(item.queued_at for item in batch),
            ),
        )

    def _send_command(self, command: str) -> None:
        if self._can_use_independent_rcon():
            if self._rcon is None:
                config = self._server.rcon
                self._rcon = PersistentRcon(
                    host=config["host"],
                    port=config["port"],
                    password=config["password"],
                    timeout=self._response_timeout,
                )
            self._rcon.command(command, timeout=self._response_timeout)
            return

        send_command = getattr(self._server, "send_command", None)
        if not callable(send_command):
            raise RuntimeError("server does not provide a command channel")
        try:
            send_command(command, response_timeout=self._response_timeout)
        except TypeError:
            send_command(command)

    def _can_use_independent_rcon(self) -> bool:
        config = getattr(self._server, "rcon", None)
        return bool(getattr(self._server, "rcon_enabled", False)) and isinstance(
            config, dict
        ) and bool(config.get("password"))

    def _close_rcon(self) -> None:
        with self._state_lock:
            rcon = self._rcon
            self._rcon = None
        if rcon is not None:
            try:
                rcon.close()
            except Exception:  # noqa: BLE001
                pass

    def _drain_unprocessed(self) -> list[_BroadcastMessage]:
        dropped: list[_BroadcastMessage] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return dropped
            dropped.append(item)
            self._queue.task_done()

    def _emit(self, event_type: str, **data: Any) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event_type, **data)
        except Exception:  # noqa: BLE001
            return

    def _build_command(self, batch: list[_BroadcastMessage]) -> str:
        components: list[dict[str, str]] = []
        for index, item in enumerate(batch):
            if index:
                components.append({"text": " | "})
            components.append({"text": item.text})
        payload = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
        return f"tellraw @a {payload}"

    def _command_size(self, batch: list[_BroadcastMessage]) -> int:
        return len(self._build_command(batch).encode("utf-8"))


__all__ = ["AsyncBroadcastDispatcher"]
