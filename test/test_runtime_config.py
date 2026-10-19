#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task-level runner runtime configuration tests."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.AgenticCache.runner import AgenticCacheRunner  # noqa: E402
from src.baseline.AutoGen.runner import AutoGenRunner  # noqa: E402
from src.baseline.BasicAsync.runner import BasicAsyncRunner  # noqa: E402
from src.baseline.BasicAsync.schemas import BasicAsyncConfig  # noqa: E402
from src.baseline.COAP.runner import COAPRunner  # noqa: E402
from src.baseline.DirectAction.runner import DirectActionRunner  # noqa: E402
from src.baseline.Parallelized.runner import PPARunner  # noqa: E402
from src.tasks import TaskSpec, apply_task_runtime_overrides  # noqa: E402


def make_spec(params: dict[str, object] | None = None) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "runtime-config-test",
            "category": "debug",
            "name": "Runtime config test",
            "agents": {"count": 1},
            "completion": {
                "timeout_seconds": 30,
                "success": {
                    "type": "survive_until",
                    "duration_seconds": 1,
                },
            },
            "params": params or {},
        }
    )


def make_session(params: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        spec=make_spec(params),
        server=None,
        memory=None,
        agents=[],
    )


class RuntimeOverrideTests(unittest.TestCase):
    def test_nested_runtime_overrides_and_flat_compatibility(self) -> None:
        nested = make_spec(
            {
                "system_messages_enabled": False,
                "system_message_interval": 40,
                "runtime": {
                    "system_messages_enabled": True,
                    "system_message_interval": 12,
                },
            }
        )
        config = apply_task_runtime_overrides(BasicAsyncConfig(), nested)
        self.assertTrue(config.system_messages_enabled)
        self.assertEqual(config.system_message_interval, 12)

        flat = make_spec(
            {
                "system_messages_enabled": True,
                "system_message_interval": 15,
            }
        )
        config = apply_task_runtime_overrides(BasicAsyncConfig(), flat)
        self.assertTrue(config.system_messages_enabled)
        self.assertEqual(config.system_message_interval, 15)

    def test_no_runtime_override_preserves_defaults(self) -> None:
        config = apply_task_runtime_overrides(BasicAsyncConfig(), make_spec())
        self.assertEqual(config, BasicAsyncConfig())

    def test_invalid_runtime_container_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "params.runtime"):
            apply_task_runtime_overrides(
                BasicAsyncConfig(), make_spec({"runtime": ["invalid"]})
            )

    def test_all_runners_receive_task_override(self) -> None:
        params = {
            "runtime": {
                "system_messages_enabled": True,
                "system_message_interval": 11,
            }
        }
        runner_types = (
            BasicAsyncRunner,
            DirectActionRunner,
            PPARunner,
            AgenticCacheRunner,
            AutoGenRunner,
            COAPRunner,
        )
        for runner_type in runner_types:
            with self.subTest(runner=runner_type.__name__):
                runner = runner_type(
                    session=make_session(params),
                    llm_client=SimpleNamespace(),
                )
                self.assertTrue(runner.config.system_messages_enabled)
                self.assertEqual(runner.config.system_message_interval, 11)


if __name__ == "__main__":
    unittest.main()
