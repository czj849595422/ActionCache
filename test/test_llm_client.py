#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/llm 客户端: 配置读取、非流式调用、错误包装与同步封装测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import (  # noqa: E402
    AgentLLMClientPool,
    GenerationMode,
    LLMClient,
    LLMConfig,
    LLMConfigurationError,
    LLMRequest,
    LLMRequestCancelled,
    LLMRequestError,
    LLMTimeoutError,
    StreamStatus,
)
from src.llm.config import MCAgent_ENV_PATH  # noqa: E402
from src.recording import (  # noqa: E402
    RecordingConfig,
    RecordingError,
    RecordingManager,
)


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


class FakeResponse:
    def __init__(self, content: str = "complete", reasoning: str = ""):
        self.model = "fake-model"
        self.usage = FakeUsage()
        self.choices = [
            SimpleNamespace(
                message=FakeMessage(content, reasoning),
                finish_reason="stop",
            )
        ]

    def model_dump(self, **_: object) -> dict[str, object]:
        return {"model": self.model, "content": self.choices[0].message.content}


class FakeCompletions:
    def __init__(self, response: FakeResponse | None = None):
        self.response = response or FakeResponse()
        self.calls: list[dict[str, object]] = []

    async def create(self, **params: object) -> object:
        self.calls.append(params)
        return self.response


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


class LLMConfigFromEnvTests(unittest.TestCase):
    def test_explicit_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "LLM_API_URL=https://example.invalid/v1/chat/completions\n"
                "LLM_API_KEY=secret-test-key\n"
                "LLM_MODEL=fake-model\n",
                encoding="utf-8",
            )
            config = LLMConfig.from_env(env)
            self.assertEqual(config.api_url, "https://example.invalid")
            self.assertEqual(config.model, "fake-model")
            self.assertNotIn("secret-test-key", repr(config))
            self.assertEqual(config.safe_dict()["api_key"], "***")

    def test_system_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "LLM_API_URL=https://file.invalid\n"
                "LLM_API_KEY=file-key\n"
                "LLM_MODEL=file-model\n",
                encoding="utf-8",
            )
            os.environ["LLM_API_URL"] = "https://sys.invalid/v1/chat/completions"
            os.environ["LLM_API_KEY"] = "sys-key"
            os.environ["LLM_MODEL"] = "sys-model"
            try:
                config = LLMConfig.from_env(env)
                self.assertEqual(config.api_url, "https://sys.invalid")
                self.assertEqual(config.model, "sys-model")
            finally:
                for key in ("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL"):
                    os.environ.pop(key, None)

    @unittest.skipUnless(MCAgent_ENV_PATH.exists(), "MCAgent .env 不存在")
    def test_fallback_to_mcagent_env(self):
        saved = {
            key: os.environ.get(key)
            for key in ("LLM_API_URL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
        }
        for key in saved:
            os.environ.pop(key, None)
        try:
            config = LLMConfig.from_env()
            self.assertTrue(config.api_url)
            self.assertTrue(config.api_key)
            self.assertTrue(config.model)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_missing_vars_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("LLM_API_URL=https://example.invalid\n", encoding="utf-8")
            with self.assertRaises(LLMConfigurationError):
                LLMConfig.from_env(env)


class LLMClientCompleteTests(unittest.TestCase):
    def test_async_complete_parses_response(self):
        completions = FakeCompletions(
            response=FakeResponse(content="ok", reasoning="")
        )
        client = LLMClient(make_config(), client=FakeClient(completions))
        response = asyncio.run(client.acomplete(make_request()))
        self.assertEqual(response.content, "ok")
        self.assertEqual(response.status, StreamStatus.COMPLETED)
        self.assertEqual(response.mode, GenerationMode.COMPLETE)
        self.assertEqual(response.usage["total_tokens"], 5)
        self.assertEqual(response.model, "fake-model")
        self.assertGreaterEqual(response.completed_at, response.started_at)
        self.assertGreaterEqual(response.elapsed_seconds, 0.0)
        self.assertIs(completions.calls[0]["stream"], False)
        self.assertEqual(completions.calls[0]["model"], "fake-model")

    def test_timeout_wrapped(self):
        class BoomCompletions(FakeCompletions):
            async def create(self, **params: object) -> object:
                raise TimeoutError("boom")

        client = LLMClient(make_config(), client=FakeClient(BoomCompletions()))
        with self.assertRaises(LLMTimeoutError):
            asyncio.run(client.acomplete(make_request()))

    def test_request_error_wrapped_and_redacted(self):
        class BoomCompletions(FakeCompletions):
            async def create(self, **params: object) -> object:
                raise RuntimeError("boom secret-test-key")

        client = LLMClient(make_config(), client=FakeClient(BoomCompletions()))
        with self.assertRaises(LLMRequestError) as ctx:
            asyncio.run(client.acomplete(make_request()))
        self.assertNotIn("secret-test-key", str(ctx.exception))

    def test_reasoning_rejected_when_thinking_disabled(self):
        completions = FakeCompletions(
            response=FakeResponse(content="ok", reasoning="think")
        )
        client = LLMClient(make_config(), client=FakeClient(completions))
        with self.assertRaises(LLMRequestError):
            asyncio.run(client.acomplete(make_request()))

    def test_sync_complete_wrapper(self):
        completions = FakeCompletions(
            response=FakeResponse(content="ok", reasoning="")
        )
        client = LLMClient(make_config(), client=FakeClient(completions))
        try:
            response = client.complete(make_request())
            self.assertEqual(response.content, "ok")
            self.assertEqual(response.usage["total_tokens"], 5)
        finally:
            client.close()

    def test_cancel_active_sync_complete(self):
        started = threading.Event()

        class BlockingCompletions(FakeCompletions):
            async def create(self, **params: object) -> object:
                self.calls.append(params)
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as tmp:
            run = RecordingManager(
                RecordingConfig(root_dir=Path(tmp))
            ).start_run("cancel-test")
            episode = run.start_episode()
            client = LLMClient(
                make_config(),
                client=FakeClient(BlockingCompletions()),
                call_recorder=episode.llm_calls,
            )
            errors: list[BaseException] = []

            def invoke() -> None:
                try:
                    client.complete(make_request("cancel"))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(client.cancel_active_requests("test_stop"), 1)
            thread.join(timeout=1)
            client.close()
            episode.finalize(status="completed")
            run.finalize()

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LLMRequestCancelled)


class AgentLLMClientPoolTests(unittest.TestCase):
    class FakeRoutedClient:
        def __init__(self, config, *, call_recorder=None) -> None:
            self.config = config
            self.call_recorder = call_recorder
            self.closed = False
            self.async_requests = []

        async def acomplete(self, request):
            self.async_requests.append(request)
            await asyncio.sleep(0)
            return self

        def close(self) -> None:
            self.closed = True

    def make_pool(self) -> AgentLLMClientPool:
        created = []

        def factory(config, *, call_recorder=None):
            client = self.FakeRoutedClient(
                config,
                call_recorder=call_recorder,
            )
            created.append(client)
            return client

        pool = AgentLLMClientPool(make_config(), client_factory=factory)
        pool.created = created  # type: ignore[attr-defined]
        return pool

    @staticmethod
    def request(agent: str, component: str, operation: str) -> LLMRequest:
        return LLMRequest(
            messages=[{"role": "user", "content": "test"}],
            metadata={
                "agent_id": agent,
                "component": component,
                "operation": operation,
            },
        )

    def test_routes_by_agent_and_call_channel(self) -> None:
        pool = self.make_pool()
        try:
            alice_standard = pool._client_for(
                self.request("Alice", "planner", "standard_action")
            )
            alice_standard_again = pool._client_for(
                self.request("Alice", "planner", "standard_action")
            )
            alice_cache = pool._client_for(
                self.request("Alice", "planner", "cache_action")
            )
            bob_standard = pool._client_for(
                self.request("Bob", "planner", "standard_action")
            )

            self.assertIs(alice_standard, alice_standard_again)
            self.assertIsNot(alice_standard, alice_cache)
            self.assertIsNot(alice_standard, bob_standard)
            self.assertEqual(len(pool.created), 3)  # type: ignore[attr-defined]
        finally:
            pool.close()

        self.assertTrue(all(client.closed for client in pool.created))  # type: ignore[attr-defined]

    def test_recorder_must_be_bound_before_first_client(self) -> None:
        pool = self.make_pool()
        recorder = object()
        pool.bind_call_recorder(recorder)
        self.assertIs(pool.call_recorder, recorder)
        pool._client_for(self.request("Alice", "planner", "standard_action"))
        with self.assertRaises(RecordingError):
            pool.bind_call_recorder(object())
        pool.close()

    def test_async_calls_use_the_same_isolation_routes(self) -> None:
        pool = self.make_pool()

        async def exercise():
            return await asyncio.gather(
                pool.acomplete(self.request("Alice", "planner", "plan")),
                pool.acomplete(self.request("Alice", "planner", "passive")),
                pool.acomplete(self.request("Bob", "planner", "plan")),
            )

        try:
            clients = asyncio.run(exercise())
            self.assertIsNot(clients[0], clients[1])
            self.assertIsNot(clients[0], clients[2])
            self.assertEqual(len(pool.created), 3)  # type: ignore[attr-defined]
            self.assertEqual(len(clients[0].async_requests), 1)
            self.assertEqual(len(clients[1].async_requests), 1)
            self.assertEqual(len(clients[2].async_requests), 1)
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
