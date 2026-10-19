"""Offline unit tests for the PPA live preflight (P0-3)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.Parallelized.preflight import (  # noqa: E402
    PreflightStatus,
    _port_in_use,
    run_preflight,
)
from src.llm import LLMConfig, LLMConfigurationError  # noqa: E402
from src.tasks import TaskSpec, TaskWorldError, TaskWorldWorkspace  # noqa: E402


VALID_PROBE_TEXT = (
    '{"skill": "wait.wait(seconds=0)", "interrupt": false, '
    '"reason": "probe"}'
)


def make_spec(agent_count: int = 2) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "preflight.test",
            "category": "test",
            "name": "Preflight Test",
            "description": "preflight unit test",
            "agents": {
                "count": agent_count,
                "names": [f"a{index}" for index in range(agent_count)],
            },
            "environment": {"world": "base_env"},
            "completion": {
                "timeout_seconds": 60,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


def build_env(
    tmp: Path,
    *,
    with_jar: bool = True,
    with_properties: bool = True,
    rcon_enabled: bool = True,
    with_world: bool = True,
) -> tuple[Path, Path]:
    server_dir = tmp / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    if with_jar:
        (server_dir / "server.jar").write_bytes(b"jar")
    if with_properties:
        (server_dir / "server.properties").write_text(
            "server-port=25565\n"
            f"enable-rcon={'true' if rcon_enabled else 'false'}\n"
            "rcon.port=25575\n"
            "rcon.password=1234\n"
            "level-name=world\n"
            "online-mode=false\n",
            encoding="utf-8",
        )
    benchmark = tmp / "benchmark"
    benchmark.mkdir(parents=True, exist_ok=True)
    if with_world:
        world = benchmark / "base_env"
        world.mkdir()
        (world / "level.dat").write_bytes(b"dat")
    return server_dir, benchmark


class FakeServer:
    def __init__(self, *, server_dir=None, log_dir=None, **kwargs) -> None:
        self.server_dir = Path(server_dir) if server_dir else Path(".")
        self.log_dir = log_dir
        self.rcon = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 25575,
            "password": "1234",
        }
        self.rcon_enabled = True
        self._started = 0
        self._stopped = 0
        self.queries: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._started > self._stopped

    def start(self) -> None:
        self._started += 1

    def stop(self, grace: int = 30) -> None:
        self._stopped += 1

    def send_command(self, command: str, response_timeout: float = 5.0) -> str:
        self.queries.append(command)
        return f"ok {command}"


class FakeWorkspace:
    def __init__(self, *, source_world=None, server_dir=None, **kwargs) -> None:
        self.source_world = Path(source_world) if source_world else None
        self.server_dir = Path(server_dir) if server_dir else None
        self.prepare_calls = 0
        self.check_calls = 0

    def check_disposable(self) -> None:
        self.check_calls += 1

    def prepare(self) -> None:
        self.prepare_calls += 1

    def cleanup(self) -> None:
        pass


class FakeLLM:
    def __init__(
        self,
        response_text: str | None = None,
        *,
        error: Exception | None = None,
        request_id: str = "probe-req-1",
    ) -> None:
        self.response_text = response_text or VALID_PROBE_TEXT
        self.error = error
        self.request_id = request_id
        self.recorder = None
        self.requests = []
        self.closed = False
        self.last_request_id: str | None = None

    def bind_call_recorder(self, recorder) -> None:
        self.recorder = recorder

    def complete(self, request):
        self.requests.append(request)
        if self.recorder is not None:
            handle = self.recorder.begin(
                request_id=request.request_id,
                mode="complete",
                source=request.source,
                request={"messages": [dict(message) for message in request.messages]},
            )
            if self.error is not None:
                handle.finish(
                    status="failed",
                    error_type=type(self.error).__name__,
                    error=str(self.error),
                )
            else:
                handle.finish(
                    status="completed",
                    response={
                        "content": self.response_text,
                        "request_id": self.request_id,
                    },
                    usage={},
                )
        if self.error is not None:
            raise self.error
        self.last_request_id = request.request_id
        return SimpleNamespace(
            content=self.response_text,
            request_id=request.request_id,
        )

    def close(self) -> None:
        self.closed = True


def fake_which(command: str) -> str | None:
    return {"java": "java", "node": "node"}.get(command)


def llm_config_factory(_) -> LLMConfig:
    return LLMConfig(
        api_url="https://example.invalid",
        api_key="secret-key",
        model="fake-model",
    )


def run_env(
    tmp: Path,
    *,
    llm_client_factory=None,
    llm_config_factory_=None,
    port_checker=None,
    command_locator=None,
    workspace_factory=None,
    java_version=None,
    mineflayer_dir=None,
    with_world: bool = True,
    rcon_enabled: bool = True,
) -> tuple:
    server_dir, benchmark = build_env(
        tmp,
        rcon_enabled=rcon_enabled,
        with_world=with_world,
    )
    llm = llm_client_factory or FakeLLM()
    workspace = workspace_factory or FakeWorkspace()
    report = run_preflight(
        spec=make_spec(2),
        server_dir=server_dir,
        bridge_port_start=3201,
        benchmark_root=benchmark,
        env_file=None,
        record_dir=tmp / "data",
        output=lambda _: None,
        server_factory=FakeServer,
        llm_config_factory=llm_config_factory_ or llm_config_factory,
        llm_client_factory=lambda _: llm,
        port_checker=port_checker or (lambda host, port: False),
        command_locator=command_locator or fake_which,
        java_version=java_version,
        world_workspace_factory=lambda **kw: workspace,
        mineflayer_dir=mineflayer_dir,
    )
    return report, llm, workspace


class PreflightReportTests(unittest.TestCase):
    def test_all_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, llm, workspace = run_env(Path(tmp))

            self.assertEqual(report.exit_code, 0)
            self.assertEqual(report.failed_checks, [])
            self.assertTrue(report.server_was_started)
            self.assertEqual(report.llm_probe_request_id, llm.last_request_id)
            self.assertEqual(workspace.prepare_calls, 0)
            self.assertGreaterEqual(workspace.check_calls, 1)
            self.assertTrue(llm.closed)
            self.assertIsNotNone(report.path)

            call_files = list((report.path / "llm" / "calls").glob("*.json"))
            self.assertEqual(len(call_files), 1)
            payload = json.loads(call_files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["request_id"], report.llm_probe_request_id)
            for file in report.path.rglob("*"):
                if file.is_file():
                    self.assertNotIn(
                        "secret-key",
                        file.read_text(encoding="utf-8", errors="replace"),
                    )

    def test_port_occupied_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(
                Path(tmp),
                port_checker=lambda host, port: True,
            )
            names = {check.name for check in report.failed_checks}
            self.assertIn("ports.minecraft", names)
            self.assertIn("ports.rcon", names)
            self.assertEqual(report.exit_code, 1)

    def test_missing_java_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(
                Path(tmp),
                command_locator=lambda command: (
                    None if command == "java" else fake_which(command)
                ),
            )
            self.assertTrue(
                any(
                    check.name == "toolchain.java"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)

    def test_old_java_is_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(
                Path(tmp),
                java_version=lambda _: (8, "1.8.0_392"),
            )
            self.assertEqual(report.exit_code, 0)
            self.assertTrue(
                any(
                    check.name == "toolchain.java"
                    and check.status == PreflightStatus.WARN
                    for check in report.checks
                )
            )

    def test_missing_node_modules_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mineflayer_dir = Path(tmp) / "mineflayer"
            mineflayer_dir.mkdir()
            report, _, _ = run_env(Path(tmp), mineflayer_dir=mineflayer_dir)
            self.assertTrue(
                any(
                    check.name == "toolchain.node_modules"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)

    def test_llm_config_missing_fails(self) -> None:
        def missing_config(_):
            raise LLMConfigurationError(
                "Missing LLM environment variables: LLM_API_URL, LLM_API_KEY, LLM_MODEL"
            )

        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(Path(tmp), llm_config_factory_=missing_config)
            self.assertTrue(
                any(
                    check.name == "llm.config"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)
            self.assertIsNotNone(report.path)
            self.assertTrue(
                any(
                    check.name == "llm.probe"
                    and check.status == PreflightStatus.SKIP
                    for check in report.checks
                )
            )

    def test_rcon_disabled_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(Path(tmp), rcon_enabled=False)
            self.assertTrue(
                any(
                    check.name == "server.rcon_config"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)

    def test_llm_probe_invalid_fails_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, llm, _ = run_env(
                Path(tmp),
                llm_client_factory=FakeLLM(response_text="not a valid decision"),
            )
            self.assertTrue(
                any(
                    check.name == "llm.probe"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)
            call_files = list((report.path / "llm" / "calls").glob("*.json"))
            self.assertEqual(len(call_files), 1)

    def test_world_source_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(Path(tmp), with_world=False)
            self.assertTrue(
                any(
                    check.name == "world.source"
                    and check.status == PreflightStatus.FAIL
                    for check in report.checks
                )
            )
            self.assertEqual(report.exit_code, 1)

    def test_collects_all_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report, _, _ = run_env(
                Path(tmp),
                with_world=False,
                port_checker=lambda host, port: True,
                command_locator=lambda command: (
                    None if command == "java" else fake_which(command)
                ),
                mineflayer_dir=Path(tmp) / "missing-mineflayer",
            )
            names = {check.name for check in report.failed_checks}
            self.assertIn("toolchain.java", names)
            self.assertIn("toolchain.node_modules", names)
            self.assertIn("world.source", names)
            self.assertIn("ports.minecraft", names)
            self.assertEqual(report.exit_code, 1)


class PreflightHelperTests(unittest.TestCase):
    def test_port_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            self.assertTrue(_port_in_use("127.0.0.1", port))
        self.assertFalse(_port_in_use("127.0.0.1", 0))

    def test_check_disposable_passes_without_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            server_dir = tmp / "server"
            server_dir.mkdir()
            (server_dir / "server.properties").write_text(
                "level-name=world\n",
                encoding="utf-8",
            )
            source = tmp / "source"
            source.mkdir()
            (source / "level.dat").write_bytes(b"")
            workspace = TaskWorldWorkspace(source_world=source, server_dir=server_dir)
            workspace.check_disposable()
            self.assertFalse(workspace.prepared)

    def test_check_disposable_detects_locked_world(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows msvcrt locking only")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            server_dir = tmp / "server"
            server_dir.mkdir()
            (server_dir / "server.properties").write_text(
                "level-name=world\n",
                encoding="utf-8",
            )
            active = server_dir / "world"
            active.mkdir()
            lock = active / "session.lock"
            lock.write_bytes(b"\x00\x00\x00\x00")
            import msvcrt

            handle = lock.open("r+b")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                source = tmp / "source"
                source.mkdir()
                (source / "level.dat").write_bytes(b"")
                workspace = TaskWorldWorkspace(
                    source_world=source,
                    server_dir=server_dir,
                )
                with self.assertRaises(TaskWorldError):
                    workspace.check_disposable()
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                handle.close()


def _load_demo():
    spec_path = ROOT / "script" / "demo_parallelized.py"
    module_spec = importlib.util.spec_from_file_location(
        "demo_parallelized",
        spec_path,
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class PreflightCliTests(unittest.TestCase):
    def test_preflight_and_check_are_mutually_exclusive(self) -> None:
        demo = _load_demo()
        with self.assertRaises(SystemExit) as ctx:
            demo.main(
                [
                    "--task",
                    "mining",
                    "--check",
                    "--preflight",
                ]
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_parser_exposes_preflight_flag(self) -> None:
        demo = _load_demo()
        args = demo.build_parser().parse_args(["--preflight"])
        self.assertTrue(args.preflight)


if __name__ == "__main__":
    unittest.main()
