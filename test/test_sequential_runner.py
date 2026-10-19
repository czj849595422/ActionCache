"""泛型顺序多 episode 运行器测试（PPA / BasicAsync 薄包装）。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.BasicAsync import (  # noqa: E402
    BasicAsyncConfig,
    BasicAsyncRunner,
    BasicAsyncSequentialRunner,
)
from src.baseline.BasicAsync.schemas import BasicAsyncRunResult  # noqa: E402
from src.baseline.Parallelized import (  # noqa: E402
    PPAConfig,
    PPARunner,
    PPASequentialRunner,
)
from src.baseline.Parallelized.schemas import PPARunResult  # noqa: E402
from src.recording import RecordingConfig, RecordingManager  # noqa: E402
from src.tasks import TaskOutcome, TaskSpec, TaskStatus  # noqa: E402


def make_spec(
    task_id: str,
    category: str = "mining",
    *,
    name: str | None = None,
) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": task_id,
            "category": category,
            "name": name or task_id,
            "description": f"{name or task_id} 描述",
            "agents": {"count": 1, "names": ["Alice"]},
            "environment": {"world": "base_env"},
            "completion": {
                "timeout_seconds": 60,
                "success": {"type": "survive_until", "duration_seconds": 10},
            },
        }
    )


def make_bundle(tmp: Path):
    return lambda spec, manager, episode: SimpleNamespace(
        spec=spec,
        manager=manager,
        server=None,
        agents=[],
        world_workspace=None,
        registry_file=tmp / "chests.json",
        close_runtime=lambda: None,
    )


class PPASequentialRunnerTests(unittest.TestCase):
    def test_runs_episodes_in_order(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("parallelized", task="sequential")
            fake_runner = SimpleNamespace(
                run=lambda: PPARunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            factory_calls: list[str] = []

            def factory(spec, manager, episode):
                factory_calls.append(spec.task_id)
                return make_bundle(Path(tmp))(spec, manager, episode)

            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=fake_runner,
            ) as from_bundle:
                sequential = PPASequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=factory,
                )
                result = sequential.run()

            self.assertEqual(factory_calls, ["clear.task", "clear.task"])
            self.assertEqual(from_bundle.call_count, 2)
            self.assertEqual(len(result.episodes), 2)
            self.assertEqual(result.episodes[0].episode_id, "1")
            self.assertEqual(result.episodes[1].episode_id, "2")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(recorder.config_values(), asdict(PPAConfig()))

            episode_dirs = sorted(
                p.name for p in recorder.path.iterdir() if p.is_dir()
            )
            self.assertEqual(episode_dirs, ["1", "2"])
            recorder.finalize()

    def test_exit_code_nonzero_when_any_episode_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("parallelized", task="sequential")
            failed = SimpleNamespace(
                run=lambda: PPARunResult(
                    outcome=TaskOutcome(TaskStatus.FAILURE)
                )
            )
            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=failed,
            ):
                sequential = PPASequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                )
                result = sequential.run()
            self.assertEqual(result.exit_code, 1)
            recorder.finalize()

    def test_runner_exception_falls_back_to_failure_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("parallelized", task="sequential")

            def exploding_run():
                raise RuntimeError("boom")

            exploding = SimpleNamespace(run=exploding_run)
            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=exploding,
            ) as from_bundle:
                sequential = PPASequentialRunner(
                    specs=[make_spec("clear.task")],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                )
                result = sequential.run()
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(len(result.episodes), 1)
            self.assertIn("boom", result.episodes[0].result.runner_error)
            self.assertEqual(from_bundle.call_count, 1)
            recorder.finalize()


class BasicAsyncSequentialRunnerTests(unittest.TestCase):
    def test_runs_episodes_in_order(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("basic_async", task="sequential")
            fake_runner = SimpleNamespace(
                run=lambda: BasicAsyncRunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            with patch.object(
                BasicAsyncRunner,
                "from_bundle",
                return_value=fake_runner,
            ) as from_bundle:
                sequential = BasicAsyncSequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=BasicAsyncConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                )
                result = sequential.run()

            self.assertEqual(from_bundle.call_count, 2)
            self.assertEqual(len(result.episodes), 2)
            self.assertEqual(result.episodes[0].episode_id, "1")
            self.assertEqual(result.episodes[1].episode_id, "2")
            self.assertEqual(result.exit_code, 0)

            episode_dirs = sorted(
                p.name for p in recorder.path.iterdir() if p.is_dir()
            )
            self.assertEqual(episode_dirs, ["1", "2"])
            recorder.finalize()

    def test_factory_resets_and_closes_client_for_each_episode(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("basic_async", task="sequential")
            clients = []

            class StrictClient:
                def __init__(self) -> None:
                    self.closed = False
                    clients.append(self)

                def bind_call_recorder(self, _recorder) -> None:
                    if self.closed:
                        raise AssertionError("closed client was reused")

                def close(self) -> None:
                    self.closed = True

            fake_runner = SimpleNamespace(
                run=lambda: BasicAsyncRunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            with patch.object(
                BasicAsyncRunner,
                "from_bundle",
                return_value=fake_runner,
            ):
                sequential = BasicAsyncSequentialRunner(
                    specs=[make_spec("clear.task"), make_spec("clear.task")],
                    llm_client_factory=StrictClient,
                    config=BasicAsyncConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                )
                result = sequential.run()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(clients), 2)
            self.assertIsNot(clients[0], clients[1])
            self.assertTrue(all(client.closed for client in clients))
            recorder.finalize()


class SequentialResumeTests(unittest.TestCase):
    def test_continues_from_start_episode_in_same_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=tmp))
            recorder = manager.start_run("parallelized", task="sequential")
            fake_runner = SimpleNamespace(
                run=lambda: PPARunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=fake_runner,
            ):
                first = PPASequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                )
                result = first.run()
            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["1", "2"],
            )
            recorder.finalize()
            run_id = recorder.context.run_id

            reopened = manager.open_run(
                "parallelized",
                task="sequential",
                run_id=run_id,
            )
            self.assertEqual(reopened.episode_count, 2)
            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=fake_runner,
            ) as from_bundle:
                resumed = PPASequentialRunner(
                    specs=[make_spec("clear.task", name="配置3")],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=reopened,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                    start_episode=3,
                )
                result = resumed.run()
            self.assertEqual(from_bundle.call_count, 1)
            self.assertEqual(result.episodes[0].episode_id, "3")
            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["3"],
            )
            episode_dirs = sorted(
                p.name for p in reopened.path.iterdir() if p.is_dir()
            )
            self.assertEqual(episode_dirs, ["1", "2", "3"])
            reopened.finalize()

    def test_start_episode_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=tmp))
            recorder = manager.start_run("parallelized", task="sequential")
            recorder.start_episode(episode_id="1").finalize(
                status="completed"
            )
            recorder.start_episode(episode_id="2").finalize(
                status="completed"
            )
            recorder.finalize()
            reopened = manager.open_run(
                "parallelized",
                task="sequential",
                run_id=recorder.context.run_id,
            )
            with self.assertRaises(ValueError):
                PPASequentialRunner(
                    specs=[make_spec("clear.task", name="配置3")],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=reopened,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                    start_episode=4,
                ).run()

    def test_episode_count_limits_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("parallelized", task="sequential")
            fake_runner = SimpleNamespace(
                run=lambda: PPARunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            with patch.object(
                PPARunner,
                "from_bundle",
                return_value=fake_runner,
            ) as from_bundle:
                sequential = PPASequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=PPAConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=make_bundle(Path(tmp)),
                    episode_count=1,
                )
                result = sequential.run()
            self.assertEqual(from_bundle.call_count, 1)
            self.assertEqual(
                [item.episode_id for item in result.episodes],
                ["1"],
            )
            recorder.finalize()


class SequentialExportsTests(unittest.TestCase):
    def test_wrappers_are_exported(self) -> None:
        from src.baseline.COAP import COAPSequentialRunner

        self.assertEqual(
            PPASequentialRunner.__mro__[1].__name__,
            "SequentialBaselineRunner",
        )
        self.assertEqual(
            BasicAsyncSequentialRunner.__mro__[1].__name__,
            "SequentialBaselineRunner",
        )
        self.assertEqual(
            COAPSequentialRunner.__mro__[1].__name__,
            "SequentialBaselineRunner",
        )


if __name__ == "__main__":
    unittest.main()
