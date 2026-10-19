#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务世界副本的覆盖部署与丢弃测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    TaskSpec,
    TaskWorldError,
    TaskWorldWorkspace,
    create_default_registry,
    resolve_task_world,
)


def make_spec(world: str = "base_env") -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "world.workspace.test",
            "category": "test",
            "name": "World Workspace Test",
            "agents": {"count": 1},
            "environment": {"world": world},
            "completion": {
                "timeout_seconds": 60,
                "success": {"type": "survive_until", "duration_seconds": 10},
            },
        }
    )


class TaskWorldWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.benchmark_root = self.root / "benchmark"
        self.source = self.benchmark_root / "base_env"
        self.source.mkdir(parents=True)
        (self.source / "level.dat").write_text("base-level", encoding="utf-8")
        (self.source / "base.txt").write_text("base", encoding="utf-8")
        self.server_dir = self.root / "server"
        self.server_dir.mkdir()
        (self.server_dir / "server.properties").write_text(
            "level-name=world\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_task_world_uses_benchmark_root(self) -> None:
        resolved = resolve_task_world(make_spec(), self.benchmark_root)
        self.assertEqual(resolved, self.source.resolve())

    def test_prepare_overwrites_world_and_cleanup_discards_copy(self) -> None:
        active = self.server_dir / "world"
        active.mkdir()
        (active / "original.txt").write_text("original", encoding="utf-8")
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )

        workspace.prepare()
        self.assertTrue(workspace.prepared)
        self.assertEqual((active / "base.txt").read_text(encoding="utf-8"), "base")
        self.assertFalse((active / "original.txt").exists())
        (active / "runtime-change.txt").write_text("discard", encoding="utf-8")

        workspace.cleanup()
        self.assertFalse(workspace.prepared)
        self.assertFalse(active.exists())
        self.assertFalse(workspace.staging_world.exists())
        self.assertFalse(workspace.legacy_backup_world.exists())
        self.assertFalse((self.source / "runtime-change.txt").exists())

    def test_cleanup_removes_copy_when_no_previous_world_exists(self) -> None:
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )
        workspace.prepare()
        self.assertTrue(workspace.active_world.exists())
        workspace.cleanup()
        self.assertFalse(workspace.active_world.exists())

    def test_copy_failure_preserves_previous_world(self) -> None:
        active = self.server_dir / "world"
        active.mkdir()
        (active / "original.txt").write_text("original", encoding="utf-8")
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )

        def fail_after_partial_copy(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "partial.txt").write_text("partial", encoding="utf-8")
            raise RuntimeError("copy failed")

        with mock.patch(
            "src.tasks.world_workspace.shutil.copytree",
            side_effect=fail_after_partial_copy,
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                workspace.prepare()

        self.assertEqual(
            (active / "original.txt").read_text(encoding="utf-8"),
            "original",
        )
        self.assertFalse(workspace.staging_world.exists())

    def test_legacy_backup_is_deleted_instead_of_restored(self) -> None:
        active = self.server_dir / "world"
        active.mkdir()
        (active / "level.dat").write_text("runtime", encoding="utf-8")
        (active / "runtime.txt").write_text("discard", encoding="utf-8")
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )
        workspace.legacy_backup_world.mkdir()
        (workspace.legacy_backup_world / "level.dat").write_text(
            "original-level",
            encoding="utf-8",
        )
        (workspace.legacy_backup_world / "original.txt").write_text(
            "original",
            encoding="utf-8",
        )

        workspace.prepare()
        self.assertEqual((active / "base.txt").read_text(encoding="utf-8"), "base")
        self.assertFalse((active / "runtime.txt").exists())
        self.assertFalse(workspace.legacy_backup_world.exists())

        workspace.cleanup()
        self.assertFalse(active.exists())

    def test_locked_active_world_is_not_removed(self) -> None:
        active = self.server_dir / "world"
        active.mkdir()
        (active / "level.dat").write_text("runtime", encoding="utf-8")
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )

        def reject_active_world(path: Path) -> None:
            if path.resolve() == active.resolve():
                raise TaskWorldError("仍被服务器占用")

        with mock.patch(
            "src.tasks.world_workspace._assert_world_unlocked",
            side_effect=reject_active_world,
        ):
            with self.assertRaisesRegex(TaskWorldError, "仍被服务器占用"):
                workspace.prepare()
        self.assertTrue(active.exists())
        self.assertFalse(workspace.staging_world.exists())

    def test_stale_staging_is_replaced(self) -> None:
        workspace = TaskWorldWorkspace(
            source_world=self.source,
            server_dir=self.server_dir,
        )
        workspace.staging_world.mkdir()
        (workspace.staging_world / "partial.txt").write_text(
            "partial",
            encoding="utf-8",
        )

        workspace.prepare()

        self.assertEqual(
            (workspace.active_world / "base.txt").read_text(encoding="utf-8"),
            "base",
        )
        self.assertFalse((workspace.active_world / "partial.txt").exists())

    def test_world_name_cannot_escape_benchmark_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "level.dat").write_text("outside", encoding="utf-8")
        with self.assertRaisesRegex(TaskWorldError, "不能超出"):
            resolve_task_world(make_spec("../outside"), self.benchmark_root)

    def test_builtin_tasks_resolve_expected_worlds(self) -> None:
        registry = create_default_registry()
        self.assertTrue(registry.list())
        self.assertEqual(
            {spec.environment.get("world") for spec in registry.list()},
            {"base_env", "trade"},
        )


if __name__ == "__main__":
    unittest.main()
