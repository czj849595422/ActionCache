#!/usr/bin/env python3
"""Server RCON 命令进程内串行化测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.launcher.mcrcon import PersistentRcon  # noqa: E402
from src.launcher.server import Server  # noqa: E402


class FakeProcess:
    is_running = True
    lines: list[str] = []


class ServerCommandLockTests(unittest.TestCase):
    def test_persistent_rcon_reuses_authenticated_client(self) -> None:
        clients = []

        class FakeSocket:
            def settimeout(self, timeout):
                return None

        class FakeMCRcon:
            def __init__(self, host, password, port, timeout):
                self.socket = FakeSocket()
                self.commands: list[str] = []
                clients.append(self)

            def connect(self):
                return None

            def disconnect(self):
                self.socket = None

            def command(self, command):
                self.commands.append(command)
                return command

        with patch("src.launcher.mcrcon.MCRcon", FakeMCRcon):
            connection = PersistentRcon(
                host="localhost",
                port=25575,
                password="secret",
            )
            self.assertEqual(connection.command("one"), "one")
            self.assertEqual(connection.command("two"), "two")
            self.assertTrue(connection.connected)
            connection.close()

        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].commands, ["one", "two"])
        self.assertFalse(connection.connected)

    def test_concurrent_rcon_commands_are_serialized(self) -> None:
        server = Server.__new__(Server)
        server._proc = FakeProcess()
        server.rcon_enabled = True
        server.rcon = {"host": "localhost", "port": 25575, "password": "secret"}
        server._command_lock = threading.RLock()
        active = 0
        max_active = 0
        state_lock = threading.Lock()
        instances = []

        class FakePersistentRcon:
            def __init__(self, **kwargs):
                instances.append(self)

            def command(self, command, timeout=None):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.02)
                with state_lock:
                    active -= 1
                return command

            def close(self):
                return None

        with patch("src.launcher.server.PersistentRcon", FakePersistentRcon):
            threads = [
                threading.Thread(target=server.send_command, args=(f"say {index}",))
                for index in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

        server._stop_command_worker()

        self.assertEqual(max_active, 1)
        self.assertEqual(len(instances), 1)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_priority_command_skips_queued_ordinary_command(self) -> None:
        server = Server.__new__(Server)
        server._proc = FakeProcess()
        server.rcon_enabled = True
        server.rcon = {"host": "localhost", "port": 25575, "password": "secret"}
        server._command_lock = threading.RLock()
        first_started = threading.Event()
        release_first = threading.Event()
        commands: list[str] = []

        class FakePersistentRcon:
            def __init__(self, **kwargs):
                return None

            def command(self, command, timeout=None):
                commands.append(command)
                if command == "normal-1":
                    first_started.set()
                    release_first.wait(timeout=1)
                return command

            def close(self):
                return None

        with patch("src.launcher.server.PersistentRcon", FakePersistentRcon):
            first = threading.Thread(
                target=server.send_command,
                args=("normal-1",),
            )
            second = threading.Thread(
                target=server.send_command,
                args=("normal-2",),
            )
            urgent = threading.Thread(
                target=server.send_priority_command,
                args=("urgent-gamemode",),
            )
            first.start()
            self.assertTrue(first_started.wait(timeout=1))
            second.start()
            deadline = time.time() + 1
            while server._command_queue.qsize() < 1 and time.time() < deadline:
                time.sleep(0.001)
            self.assertGreaterEqual(server._command_queue.qsize(), 1)
            urgent.start()
            release_first.set()
            for thread in (first, second, urgent):
                thread.join(timeout=1)

        server._stop_command_worker()

        self.assertEqual(commands, ["normal-1", "urgent-gamemode", "normal-2"])
        self.assertTrue(all(not thread.is_alive() for thread in (first, second, urgent)))

    def test_commands_reject_submission_after_dispatcher_stops(self) -> None:
        server = Server.__new__(Server)
        server._proc = FakeProcess()
        server.rcon_enabled = True
        server.rcon = {"host": "localhost", "port": 25575, "password": "secret"}
        server._command_lock = threading.RLock()
        server._ensure_command_state()
        server._command_worker_stop.set()

        with self.assertRaisesRegex(RuntimeError, "调度器已停止"):
            server.send_command("say after-stop")


if __name__ == "__main__":
    unittest.main()
