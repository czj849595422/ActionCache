#!/usr/bin/env python3
"""Fail-closed automatic LLM recording tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import LLMClient, LLMConfig, LLMRequest  # noqa: E402
from src.recording import (  # noqa: E402
    LLMCallSource,
    RecordingConfig,
    RecordingError,
    RecordingManager,
)


class Dumpable:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)

    def model_dump(self, **_: object) -> dict[str, object]:
        return dict(self.__dict__)


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.evidence_dir: Path | None = None
        self.evidence_suffix: str | None = None
        self.started_seen: str | None = None

    async def create(self, **params: object) -> object:
        self.calls.append(params)
        if self.evidence_dir is not None and self.evidence_suffix is not None:
            matches = list(self.evidence_dir.glob(f"*.{self.evidence_suffix}.json"))
            if not matches:
                raise FileNotFoundError("no started LLM call artifact found")
            self.started_seen = json.loads(matches[0].read_text(encoding="utf-8"))["status"]
        return Dumpable(
            model="fake-model",
            usage=Dumpable(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            choices=[
                SimpleNamespace(
                    message=Dumpable(content="ok", reasoning_content=""),
                    finish_reason="stop",
                )
            ],
        )


def config() -> LLMConfig:
    return LLMConfig(
        api_url="https://example.invalid",
        api_key="secret-key",
        model="fake-model",
        max_retries=0,
    )


class LLMRecordingTests(unittest.TestCase):
    def make_external(self, root: Path):
        run = RecordingManager(
            RecordingConfig(root_dir=root, redact_values=("secret-key",))
        ).start_run("parallelized")
        episode = run.start_episode()
        return run, episode

    def test_complete_records_source_input_output_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run, episode = self.make_external(Path(tmp))
            completions = FakeCompletions()
            client = LLMClient(
                config(),
                client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                call_recorder=episode.llm_calls,
            )
            request = LLMRequest(
                request_id="complete-1",
                messages=[{"role": "user", "content": "hello"}],
                extra_headers={"Authorization": "Bearer secret-key"},
                source=LLMCallSource(
                    component="planning_worker",
                    operation="plan",
                    task_id="task-a",
                    agent_id="a0",
                ),
            )
            completions.evidence_dir = episode.path / "llm" / "calls"
            completions.evidence_suffix = "a0.plan.001.complete"
            response = asyncio.run(client.acomplete(request))
            self.assertEqual(response.content, "ok")
            call_path = next(
                (episode.path / "llm" / "calls").glob("*.a0.plan.001.complete.json")
            )
            payload = json.loads(call_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(completions.started_seen, "started")
            self.assertEqual(payload["source"]["component"], "planning_worker")
            self.assertEqual(payload["request"]["messages"][0]["content"], "hello")
            self.assertEqual(payload["request"]["extra_header_names"], ["Authorization"])
            self.assertNotIn("secret-key", call_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["response"]["content"], "ok")
            self.assertEqual(payload["usage"]["normalized"]["total_tokens"], 6)
            episode.finalize(status="completed")
            run.finalize()

    def test_implicit_standalone_recording_is_finalized_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completions = FakeCompletions()
            client = LLMClient(
                config(),
                client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                recording_config=RecordingConfig(root_dir=Path(tmp)),
            )
            response = client.complete(
                LLMRequest(
                    request_id="implicit-1",
                    messages=[{"role": "user", "content": "hello"}],
                )
            )
            client.close()
            self.assertEqual(response.content, "ok")
            run_dirs = list((Path(tmp) / "standalone").iterdir())
            self.assertEqual(len(run_dirs), 1)
            episode = run_dirs[0] / "episode-0001"
            self.assertTrue(
                any(
                    (episode / "llm" / "calls").glob(
                        "*.agent.complete.001.implicit.json"
                    )
                )
            )
            self.assertTrue((episode / "llm" / "summary.json").is_file())
            summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "completed")

    def test_started_failure_prevents_provider_call(self) -> None:
        class BrokenRecorder:
            def begin(self, **_: object) -> object:
                raise RecordingError("disk unavailable")

        completions = FakeCompletions()
        client = LLMClient(
            config(),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            call_recorder=BrokenRecorder(),  # type: ignore[arg-type]
        )
        with self.assertRaises(RecordingError):
            asyncio.run(client.acomplete(LLMRequest(messages=[{"role": "user", "content": "x"}])))
        self.assertEqual(completions.calls, [])

    def test_terminal_recording_failure_does_not_return_success(self) -> None:
        class BrokenHandle:
            def finish(self, **_: object) -> None:
                raise RecordingError("replace failed")

        class StartedRecorder:
            def begin(self, **_: object) -> BrokenHandle:
                return BrokenHandle()

        completions = FakeCompletions()
        client = LLMClient(
            config(),
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            call_recorder=StartedRecorder(),  # type: ignore[arg-type]
        )
        with self.assertRaises(RecordingError):
            asyncio.run(client.acomplete(LLMRequest(messages=[{"role": "user", "content": "x"}])))
        self.assertEqual(len(completions.calls), 1)

    def test_recorder_cannot_be_rebound_after_first_call(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _, episode_a = self.make_external(Path(first))
            _, episode_b = self.make_external(Path(second))
            completions = FakeCompletions()
            client = LLMClient(
                config(),
                client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                call_recorder=episode_a.llm_calls,
            )
            asyncio.run(client.acomplete(LLMRequest(messages=[{"role": "user", "content": "x"}])))
            with self.assertRaises(RecordingError):
                client.bind_call_recorder(episode_b.llm_calls)


if __name__ == "__main__":
    unittest.main()
