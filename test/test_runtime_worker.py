#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/runtime 后台循环 worker 测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import LoopWorker  # noqa: E402


class _CountingWorker(LoopWorker):
    def __init__(self, target: int | None = None, **kwargs: object):
        super().__init__(**kwargs)
        self.target = target
        self.values: list[int] = []
        self.setup_count = 0
        self.teardown_count = 0

    def setup(self) -> None:
        self.setup_count += 1

    def iterate(self) -> None:
        self.values.append(len(self.values) + 1)
        if self.target is not None and len(self.values) >= self.target:
            self.stop(reason="reached_target")

    def teardown(self) -> None:
        self.teardown_count += 1


class _FailingWorker(LoopWorker):
    def __init__(self, **kwargs: object):
        super().__init__(**kwargs)
        self.on_error_calls: list[tuple[Exception, LoopWorker]] = []

    def setup(self) -> None:
        self.on_error_calls = []

    def iterate(self) -> None:
        raise RuntimeError("boom")


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class LoopWorkerTests(unittest.TestCase):
    def test_iterates_and_stops_itself(self) -> None:
        worker = _CountingWorker(target=3, name="counter")
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.iteration_count >= 3))
        self.assertTrue(_wait_until(lambda: not worker.is_running))
        self.assertEqual(worker.stop_reason, "reached_target")
        self.assertEqual(worker.setup_count, 1)
        self.assertEqual(worker.teardown_count, 1)
        self.assertEqual(worker.values, [1, 2, 3])

    def test_stop_is_idempotent_and_keeps_first_reason(self) -> None:
        worker = _CountingWorker(name="counter")
        worker.start()
        self.assertTrue(worker.wait_ready(timeout=2.0))
        worker.stop(reason="first")
        worker.stop(reason="second")
        self.assertEqual(worker.stop_reason, "first")
        self.assertFalse(worker.is_running)

    def test_external_stop_joins_thread(self) -> None:
        worker = _CountingWorker(loop_interval=0.05, name="counter")
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.iteration_count >= 1))
        worker.stop(reason="external", timeout=3.0)
        self.assertFalse(worker.is_running)
        self.assertEqual(worker.stop_reason, "external")

    def test_iterate_exception_triggers_on_error_and_stops(self) -> None:
        errors: list[tuple[Exception, LoopWorker]] = []

        def on_error(exc: Exception, worker: LoopWorker) -> None:
            errors.append((exc, worker))

        worker = _FailingWorker(name="failing", on_error=on_error)
        worker.start()
        self.assertTrue(_wait_until(lambda: not worker.is_running))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][0], RuntimeError)
        self.assertIsInstance(worker.last_error, RuntimeError)
        self.assertEqual(worker.stop_reason, "error: RuntimeError")

    def test_start_twice_raises(self) -> None:
        worker = _CountingWorker(name="counter")
        worker.start()
        with self.assertRaises(RuntimeError):
            worker.start()
        worker.stop()

    def test_restart_after_stop_raises(self) -> None:
        worker = _CountingWorker(name="counter")
        worker.start()
        worker.stop()
        with self.assertRaises(RuntimeError):
            worker.start()

    def test_wait_ready(self) -> None:
        worker = _CountingWorker(name="counter")
        self.assertFalse(worker.ready)
        worker.start()
        self.assertTrue(worker.wait_ready(timeout=2.0))
        worker.stop()

    def test_loop_interval_limits_iteration_rate(self) -> None:
        worker = _CountingWorker(loop_interval=0.1, name="slow")
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.iteration_count >= 1))
        start = time.monotonic()
        self.assertTrue(_wait_until(lambda: worker.iteration_count >= 2))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.05)
        worker.stop()


if __name__ == "__main__":
    unittest.main()
