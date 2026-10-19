#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/llm 保存功能: 时间后缀、冲突处理与记录内容测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import GenerationMode, LLMResponse, StreamStatus, save_call  # noqa: E402


def make_response(**overrides: object) -> LLMResponse:
    values: dict[str, object] = {
        "request_id": "r1",
        "mode": GenerationMode.COMPLETE,
        "status": StreamStatus.COMPLETED,
        "content": "hello",
        "reasoning_content": "",
        "model": "fake-model",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        "started_at": 100.0,
        "completed_at": 101.0,
        "keyword_events": [],
        "metadata": {},
    }
    values.update(overrides)
    return LLMResponse(**values)  # type: ignore[arg-type]


class SaveCallTests(unittest.TestCase):
    def test_save_without_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_call(make_response(), Path(tmp), "call.json")
            self.assertEqual(path.name, "call.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["content"], "hello")
            self.assertEqual(data["usage"]["total_tokens"], 5)
            self.assertEqual(data["elapsed_seconds"], 1.0)
            self.assertEqual(data["status"], "completed")

    def test_save_with_timestamp_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_call(
                make_response(), Path(tmp), "call.json", with_timestamp=True
            )
            self.assertRegex(path.name, r"^call_\d{8}-\d{6}\.json$")

    def test_collision_appends_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = save_call(make_response(), Path(tmp), "call.json")
            second = save_call(make_response(), Path(tmp), "call.json")
            self.assertEqual(first.name, "call.json")
            self.assertEqual(second.name, "call-2.json")

    def test_messages_included_when_provided(self):
        messages = [{"role": "user", "content": "hi"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = save_call(
                make_response(), Path(tmp), "call.json", messages=messages
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["messages"], messages)

    def test_directory_created_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            path = save_call(make_response(), target, "call.json")
            self.assertTrue(path.parent.exists())

    def test_keyword_events_persisted(self):
        response = make_response(
            keyword_events=[{"trigger_name": "t", "triggered_at": 100.5}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = save_call(response, Path(tmp), "call.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["keyword_events"][0]["trigger_name"], "t")

    def test_call_record_dict_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_call(
                {"request_id": "x", "status": "completed", "content": "ok"},
                Path(tmp),
                "raw.json",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["content"], "ok")

    def test_invalid_record_type_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TypeError):
                save_call("not-a-record", Path(tmp), "bad.json")


if __name__ == "__main__":
    unittest.main()
