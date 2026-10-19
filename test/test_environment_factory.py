#!/usr/bin/env python3
"""任务环境工厂的服务器实例隔离测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import TaskSpec, create_task_environment_bundle  # noqa: E402


class FakeServer:
    def __init__(self, *, server_dir, log_dir=None) -> None:
        self.server_dir = Path(server_dir)


class FakeAgent:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.username = kwargs["username"]


class FakeWorkspace:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def make_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "debug",
            "category": "debug",
            "name": "Factory Test",
            "agents": {"count": 2, "names": ["Alice", "Bob"]},
            "completion": {
                "timeout_seconds": 10,
                "success": {"type": "survive_until", "duration_seconds": 1},
            },
        }
    )


class EnvironmentFactoryTests(unittest.TestCase):
    def test_passes_server_port_to_each_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_dir = root / "minecraft_2"
            server_dir.mkdir()
            (server_dir / "server.properties").write_text(
                "server-port=25566\nrcon.port=25576\n",
                encoding="utf-8",
            )
            bundle = create_task_environment_bundle(
                spec=make_spec(),
                manager=object(),
                source_world=root / "benchmark",
                server_dir=server_dir,
                runtime_root=root / "runtime",
                server_factory=FakeServer,
                agent_factory=FakeAgent,
                workspace_factory=FakeWorkspace,
            )
            try:
                self.assertEqual(
                    [agent.kwargs["port"] for agent in bundle.agents],
                    [25566, 25566],
                )
                self.assertEqual(
                    [agent.kwargs["bridge_port"] for agent in bundle.agents],
                    [3201, 3202],
                )
            finally:
                bundle.close_runtime()


if __name__ == "__main__":
    unittest.main()
