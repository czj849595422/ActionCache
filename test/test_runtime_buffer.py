#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/runtime 单槽覆盖队列测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import SingleSlotOverwriteBuffer  # noqa: E402


class SingleSlotOverwriteBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = SingleSlotOverwriteBuffer()

    def test_put_overwrites_and_returns_increasing_versions(self) -> None:
        first = self.buffer.put({"action": "a"})
        second = self.buffer.put({"action": "b"})
        self.assertGreater(second, first)
        self.assertEqual(self.buffer.peek(), (second, {"action": "b"}))

    def test_peek_empty_returns_none(self) -> None:
        self.assertIsNone(self.buffer.peek())
        self.assertEqual(self.buffer.version, 0)

    def test_has_new_distinguishes_new_and_seen(self) -> None:
        self.assertFalse(self.buffer.has_new(0))
        version = self.buffer.put("x")
        self.assertTrue(self.buffer.has_new(0))
        self.assertTrue(self.buffer.has_new(version - 1))
        self.assertFalse(self.buffer.has_new(version))
        self.assertFalse(self.buffer.has_new(version + 1))

    def test_clear_empties_and_future_put_is_detected(self) -> None:
        version = self.buffer.put("x")
        self.buffer.clear()
        self.assertIsNone(self.buffer.peek())
        self.assertFalse(self.buffer.has_new(version))
        new_version = self.buffer.put("y")
        self.assertGreater(new_version, version)
        self.assertTrue(self.buffer.has_new(version))

    def test_put_none_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.buffer.put(None)

    def test_wait_new_returns_true_when_item_arrives(self) -> None:
        def producer() -> None:
            time.sleep(0.05)
            self.buffer.put("ready")

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        self.assertTrue(self.buffer.wait_new(0, timeout=2.0))
        self.assertEqual(self.buffer.peek(), (1, "ready"))
        thread.join(timeout=2.0)

    def test_wait_new_times_out(self) -> None:
        self.assertFalse(self.buffer.wait_new(0, timeout=0.05))

    def test_concurrent_puts_are_serialized(self) -> None:
        threads = [
            threading.Thread(
                target=lambda: self.buffer.put(threading.get_ident()),
                daemon=True,
            )
            for _ in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        self.assertEqual(self.buffer.version, 20)
        self.assertIsNotNone(self.buffer.peek())


if __name__ == "__main__":
    unittest.main()
