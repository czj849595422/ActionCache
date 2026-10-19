#!/usr/bin/env python3
"""Tests for asynchronous batched public-chat delivery."""

from __future__ import annotations

from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from src.launcher.broadcast_dispatcher import AsyncBroadcastDispatcher


class AsyncBroadcastDispatcherTests(unittest.TestCase):
    def test_enqueue_returns_without_waiting_for_rcon(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class FakeRcon:
            def __init__(self, **kwargs):
                pass

            def command(self, command, timeout=None):
                started.set()
                release.wait(timeout=1)

            def close(self):
                pass

        server = SimpleNamespace(
            rcon_enabled=True,
            rcon={"host": "localhost", "port": 25575, "password": "secret"},
        )
        with patch(
            "src.launcher.broadcast_dispatcher.PersistentRcon", FakeRcon
        ):
            dispatcher = AsyncBroadcastDispatcher(
                server,
                batch_window_seconds=0,
                response_timeout=1,
            )
            started_at = time.monotonic()
            self.assertTrue(dispatcher.enqueue("first"))
            self.assertLess(time.monotonic() - started_at, 0.1)
            self.assertTrue(started.wait(timeout=1))
            release.set()
            dispatcher.close(timeout=1)

    def test_messages_are_sent_as_one_batched_command(self) -> None:
        commands: list[str] = []
        sent = threading.Event()

        class FakeRcon:
            def __init__(self, **kwargs):
                pass

            def command(self, command, timeout=None):
                commands.append(command)
                sent.set()

            def close(self):
                pass

        events: list[tuple[str, dict]] = []
        server = SimpleNamespace(
            rcon_enabled=True,
            rcon={"host": "localhost", "port": 25575, "password": "secret"},
        )
        with patch(
            "src.launcher.broadcast_dispatcher.PersistentRcon", FakeRcon
        ):
            dispatcher = AsyncBroadcastDispatcher(
                server,
                event_sink=lambda event_type, **data: events.append(
                    (event_type, data)
                ),
                batch_window_seconds=0.05,
            )
            dispatcher.enqueue("one")
            dispatcher.enqueue("two")
            self.assertTrue(sent.wait(timeout=1))
            dispatcher.close(timeout=1)

        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("tellraw @a "))
        self.assertIn("one", commands[0])
        self.assertIn("two", commands[0])
        queued = [data for event, data in events if event == "broadcast_queued"]
        sent_events = [data for event, data in events if event == "broadcast_sent"]
        self.assertEqual(len(queued), 2)
        self.assertEqual(sent_events[0]["message_count"], 2)

    def test_send_failure_is_recorded_without_raising_to_caller(self) -> None:
        events: list[tuple[str, dict]] = []

        class FakeRcon:
            def __init__(self, **kwargs):
                pass

            def command(self, command, timeout=None):
                raise RuntimeError("rcon failed")

            def close(self):
                pass

        server = SimpleNamespace(
            rcon_enabled=True,
            rcon={"host": "localhost", "port": 25575, "password": "secret"},
        )
        with patch(
            "src.launcher.broadcast_dispatcher.PersistentRcon", FakeRcon
        ):
            dispatcher = AsyncBroadcastDispatcher(
                server,
                event_sink=lambda event_type, **data: events.append(
                    (event_type, data)
                ),
                batch_window_seconds=0,
            )
            self.assertTrue(dispatcher.enqueue("message"))
            dispatcher.close(timeout=1)

        failures = [data for event, data in events if event == "broadcast_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "send_error")


if __name__ == "__main__":
    unittest.main()
