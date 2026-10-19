#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/llm 流式会话: 关键词触发、部分返回、统计与停止测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import (  # noqa: E402
    LLMClient,
    LLMConfig,
    LLMRequest,
    StreamStatus,
    StreamEventType,
    StreamTrigger,
    TriggerNotFoundError,
)
from src.recording import RecordingConfig, RecordingManager  # noqa: E402


class FakeMessage:
    def __init__(self, content: str, reasoning_content: str = ""):
        self.content = content
        self.reasoning_content = reasoning_content

    def model_dump(self, **_: object) -> dict[str, object]:
        return {
            "content": self.content,
            "reasoning_content": self.reasoning_content,
        }


class FakeUsage:
    def __init__(self, prompt_tokens: int = 2, completion_tokens: int = 3):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def model_dump(self, **_: object) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class FakeDelta:
    def __init__(self, content: str = "", reasoning_content: str = ""):
        self.content = content
        self.reasoning_content = reasoning_content

    def model_dump(self, **_: object) -> dict[str, str]:
        return {
            "content": self.content,
            "reasoning_content": self.reasoning_content,
        }


class FakeChunk:
    def __init__(
        self,
        content: str = "",
        *,
        reasoning: str = "",
        finish_reason: str | None = None,
        usage: FakeUsage | None = None,
        with_choice: bool = True,
    ):
        self.model = "fake-model"
        self.usage = usage
        self.choices = (
            [
                SimpleNamespace(
                    delta=FakeDelta(content, reasoning),
                    finish_reason=finish_reason,
                )
            ]
            if with_choice
            else []
        )

    def model_dump(self, **_: object) -> dict[str, object]:
        return {"model": self.model, "choices": len(self.choices)}


class FakeAsyncStream:
    def __init__(self, chunks: list[object], delays: list[float] | None = None):
        self.chunks = chunks
        self.delays = delays or [0.0] * len(chunks)
        self.index = 0
        self.consumed = 0
        self.closed = False
        self.close_count = 0

    def __aiter__(self) -> "FakeAsyncStream":
        return self

    async def __anext__(self) -> object:
        if self.closed or self.index >= len(self.chunks):
            raise StopAsyncIteration
        delay = self.delays[self.index]
        if delay:
            await asyncio.sleep(delay)
        if self.closed:
            raise StopAsyncIteration
        item = self.chunks[self.index]
        self.index += 1
        if isinstance(item, BaseException):
            raise item
        self.consumed += 1
        return item

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


class FakeCompletions:
    def __init__(self, streams: list[FakeAsyncStream] | None = None):
        self.streams = list(streams or [])
        self.calls: list[dict[str, object]] = []

    async def create(self, **params: object) -> object:
        self.calls.append(params)
        if not self.streams:
            raise RuntimeError("No fake stream configured")
        return self.streams.pop(0)


class FakeClient:
    def __init__(self, completions: FakeCompletions):
        self.chat = SimpleNamespace(completions=completions)


def make_config(**overrides: object) -> LLMConfig:
    values: dict[str, object] = {
        "api_url": "https://example.invalid/v1/chat/completions",
        "api_key": "secret-test-key",
        "model": "fake-model",
        "max_retries": 0,
    }
    values.update(overrides)
    return LLMConfig(**values)  # type: ignore[arg-type]


def make_request(text: str = "hello") -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}])


def _start(
    chunks: list[object],
    triggers: list[StreamTrigger],
    *,
    config: LLMConfig | None = None,
    delays: list[float] | None = None,
    call_recorder=None,
):
    stream = FakeAsyncStream(chunks, delays=delays)
    completions = FakeCompletions(streams=[stream])
    client = LLMClient(
        config or make_config(),
        client=FakeClient(completions),
        call_recorder=call_recorder,
    )
    return client, completions, stream


class StreamingSessionTests(unittest.TestCase):
    def test_stream_completion_and_stop_are_recorded_once(self):
        async def scenario(root: Path) -> None:
            run = RecordingManager(
                RecordingConfig(root_dir=root)
            ).start_run("standalone")
            episode = run.start_episode()
            client, _, _ = _start(
                chunks=[
                    FakeChunk(content="hello"),
                    FakeChunk(with_choice=False, usage=FakeUsage(4, 6)),
                ],
                triggers=[],
                config=make_config(stream_include_usage=True),
                call_recorder=episode.llm_calls,
            )
            completed = await client.astart_stream(
                LLMRequest(
                    request_id="stream-completed",
                    messages=[{"role": "user", "content": "hello"}],
                )
            )
            await completed.wait_finished(timeout=1)

            client_two, _, _ = _start(
                chunks=[FakeChunk(content="a"), FakeChunk(content="b")],
                triggers=[],
                delays=[0.05, 0.05],
                call_recorder=episode.llm_calls,
            )
            stopped = await client_two.astart_stream(
                LLMRequest(
                    request_id="stream-stopped",
                    messages=[{"role": "user", "content": "stop"}],
                )
            )
            await asyncio.sleep(0.01)
            await stopped.stop(reason="test_stop")

            calls = episode.path / "llm" / "calls"
            completed_payload = json.loads(
                next(calls.glob("*.agent.stream.001.stream-c.json")).read_text(
                    encoding="utf-8"
                )
            )
            stopped_payload = json.loads(
                next(calls.glob("*.agent.stream.002.stream-s.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(completed_payload["status"], "completed")
            self.assertEqual(completed_payload["response"]["content"], "hello")
            self.assertEqual(
                completed_payload["usage"]["normalized"]["total_tokens"], 10
            )
            self.assertEqual(stopped_payload["status"], "stopped")
            self.assertEqual(stopped_payload["response"]["stop_reason"], "test_stop")
            self.assertEqual(len(list(calls.glob("*.json"))), 2)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))

    def test_keyword_spans_chunks_and_stream_continues(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[
                    FakeChunk(content="alpha"),
                    FakeChunk(content="<SPLIT>"),
                    FakeChunk(content="beta"),
                ],
                triggers=[StreamTrigger(name="split", keyword="<SPLIT>")],
            )
            session = await client.astart_stream(
                make_request(),
                triggers=[StreamTrigger(name="split", keyword="<SPLIT>")],
            )
            trigger = await session.wait_for_trigger("split", timeout=1)
            self.assertEqual(trigger.segment, "alpha")
            self.assertEqual(trigger.accumulated_content, "alpha")
            response = await session.wait_finished(timeout=1)
            self.assertEqual(response.content, "alpha<SPLIT>beta")
            self.assertEqual(response.status, StreamStatus.COMPLETED)
            self.assertEqual(response.keyword_events[0]["trigger_name"], "split")

        asyncio.run(scenario())

    def test_repeated_keywords_return_incremental_segments(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[FakeChunk(content="part1<PART>part2<PART>part3")],
                triggers=[
                    StreamTrigger(
                        name="part", keyword="<PART>", emit_once=False
                    )
                ],
            )
            session = await client.astart_stream(
                make_request(),
                triggers=[
                    StreamTrigger(name="part", keyword="<PART>", emit_once=False)
                ],
            )
            first = await session.wait_for_trigger("part", occurrence=1, timeout=1)
            second = await session.wait_for_trigger("part", occurrence=2, timeout=1)
            self.assertEqual(first.occurrence, 1)
            self.assertEqual(first.segment, "part1")
            self.assertEqual(second.occurrence, 2)
            self.assertEqual(second.segment, "part2")
            self.assertEqual(
                second.accumulated_content, "part1<PART>part2"
            )
            self.assertGreaterEqual(second.triggered_at, first.triggered_at)
            await session.wait_finished(timeout=1)

        asyncio.run(scenario())

    def test_case_insensitive_trigger(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[FakeChunk(content="Before<end>After")],
                triggers=[
                    StreamTrigger(name="end", keyword="<END>", case_sensitive=False)
                ],
            )
            session = await client.astart_stream(
                make_request(),
                triggers=[
                    StreamTrigger(name="end", keyword="<END>", case_sensitive=False)
                ],
            )
            trigger = await session.wait_for_trigger("end", timeout=1)
            self.assertEqual(trigger.segment, "Before")
            self.assertEqual(trigger.accumulated_content, "Before")
            await session.wait_finished(timeout=1)

        asyncio.run(scenario())

    def test_trigger_can_include_keyword(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[FakeChunk(content="alpha<SPLIT>beta")],
                triggers=[
                    StreamTrigger(
                        name="split", keyword="<SPLIT>", include_keyword=True
                    )
                ],
            )
            session = await client.astart_stream(
                make_request(),
                triggers=[
                    StreamTrigger(
                        name="split", keyword="<SPLIT>", include_keyword=True
                    )
                ],
            )
            trigger = await session.wait_for_trigger("split", timeout=1)
            self.assertEqual(trigger.segment, "alpha<SPLIT>")
            await session.wait_finished(timeout=1)

        asyncio.run(scenario())

    def test_usage_only_chunk_is_retained(self):
        async def scenario() -> None:
            client, completions, _ = _start(
                chunks=[
                    FakeChunk(content="hi"),
                    FakeChunk(with_choice=False, usage=FakeUsage(4, 6)),
                ],
                triggers=[],
                config=make_config(stream_include_usage=True),
            )
            session = await client.astart_stream(make_request())
            response = await session.wait_finished(timeout=1)
            self.assertEqual(response.usage["total_tokens"], 10)
            self.assertEqual(
                completions.calls[0]["stream_options"], {"include_usage": True}
            )

        asyncio.run(scenario())

    def test_stop_is_idempotent_and_closes_stream_once(self):
        async def scenario() -> None:
            client, _, stream = _start(
                chunks=[FakeChunk(content="a"), FakeChunk(content="b")],
                triggers=[],
                delays=[0.05, 0.05],
            )
            session = await client.astart_stream(make_request())
            await asyncio.sleep(0.01)
            first = await session.stop(reason="test_stop")
            self.assertEqual(first.status, StreamStatus.STOPPED)
            self.assertEqual(first.stop_reason, "test_stop")
            second = await session.stop()
            self.assertEqual(second.status, StreamStatus.STOPPED)
            self.assertEqual(stream.close_count, 1)
            self.assertEqual(session.status, StreamStatus.STOPPED)

        asyncio.run(scenario())

    def test_threading_stop_event_closes_stream(self):
        async def scenario() -> None:
            client, _, stream = _start(
                chunks=[FakeChunk(content="a"), FakeChunk(content="b")],
                triggers=[],
                delays=[1.0, 1.0],
            )
            stop_event = threading.Event()
            session = await client.astart_stream(
                make_request("thread-stop"),
                stop_event=stop_event,
            )
            await asyncio.sleep(0.01)
            stop_event.set()
            response = await session.wait_finished(timeout=1)
            self.assertEqual(response.status, StreamStatus.STOPPED)
            self.assertEqual(stream.close_count, 1)

        asyncio.run(scenario())

    def test_sync_stream_stop_event_unblocks_iterator(self):
        client, _, stream = _start(
            chunks=[FakeChunk(content="a"), FakeChunk(content="b")],
            triggers=[],
            delays=[1.0, 1.0],
        )
        stop_event = threading.Event()
        events = []

        def consume() -> None:
            events.extend(client.stream(make_request("sync-thread-stop"), stop_event=stop_event))

        thread = threading.Thread(target=consume)
        thread.start()
        time.sleep(0.02)
        stop_event.set()
        thread.join(timeout=1)
        client.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(events[-1].type, StreamEventType.STOPPED)
        self.assertEqual(stream.close_count, 1)

    def test_failure_preserves_partial_output_and_redacts_key(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[
                    FakeChunk(content="partial"),
                    RuntimeError("boom secret-test-key"),
                ],
                triggers=[],
            )
            session = await client.astart_stream(make_request())
            response = await session.wait_finished(timeout=1)
            self.assertEqual(response.status, StreamStatus.FAILED)
            self.assertEqual(response.content, "partial")
            self.assertIn("boom", response.error or "")
            self.assertNotIn("secret-test-key", response.error or "")

        asyncio.run(scenario())

    def test_missing_trigger_is_reported_after_completion(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[FakeChunk(content="hello")],
                triggers=[StreamTrigger(name="missing", keyword="<MISSING>")],
            )
            session = await client.astart_stream(
                make_request(),
                triggers=[StreamTrigger(name="missing", keyword="<MISSING>")],
            )
            with self.assertRaises(TriggerNotFoundError):
                await session.wait_for_trigger("missing", timeout=1)

        asyncio.run(scenario())

    def test_events_sequence_ends_with_terminal(self):
        async def scenario() -> None:
            client, _, _ = _start(
                chunks=[FakeChunk(content="ok")],
                triggers=[],
            )
            session = await client.astart_stream(make_request())
            types = [event.type.value async for event in session.events()]
            self.assertIn("started", types)
            self.assertIn("content_delta", types)
            self.assertEqual(types[-1], "completed")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
