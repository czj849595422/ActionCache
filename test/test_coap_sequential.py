"""COAP 顺序多 episode 运行器测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.COAP import (  # noqa: E402
    CacheBootConfig,
    CacheBootMode,
    COAPConfig,
    COAPRunner,
    COAPSequentialRunner,
)
from src.baseline.COAP.schemas import COAPRunResult  # noqa: E402
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


class COAPSequentialRunnerTests(unittest.TestCase):
    def test_program_error_retries_same_episode_and_archives_attempt(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            attempts: list[Path] = []

            def from_bundle(bundle, **kwargs):
                episode_path = Path(kwargs["episode_recorder"].path)
                attempts.append(episode_path)
                if len(attempts) == 1:
                    return SimpleNamespace(
                        run=lambda: COAPRunResult(runner_error="worker disconnected")
                    )
                return SimpleNamespace(
                    run=lambda: COAPRunResult(
                        outcome=TaskOutcome(TaskStatus.SUCCESS)
                    )
                )

            with patch.object(COAPRunner, "from_bundle", side_effect=from_bundle):
                sequential = COAPSequentialRunner(
                    specs=[make_spec("clear.task")],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=COAPConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=lambda spec, manager, episode: SimpleNamespace(
                        spec=spec,
                        manager=manager,
                        server=None,
                        agents=[],
                        world_workspace=None,
                        registry_file=Path(tmp) / "chests.json",
                        close_runtime=lambda: None,
                    ),
                )
                result = sequential.run()

            self.assertEqual(result.process_exit_code, 0)
            self.assertEqual(len(result.episodes), 1)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], recorder.path / "1")
            self.assertEqual(attempts[1], recorder.path / "1")
            episode_entries = {
                p.name for p in (recorder.path / "1").iterdir()
            }
            self.assertTrue({"attempt-1", "episode.json", "result.json"}.issubset(
                episode_entries
            ))
            archived_result = json.loads(
                (recorder.path / "1" / "attempt-1" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                archived_result["result"]["runner_error"],
                "worker disconnected",
            )
            self.assertEqual(recorder.episode_count, 1)
            recorder.finalize()

    def test_runs_episodes_in_order(self) -> None:
        spec_a = make_spec("clear.task", name="配置1")
        spec_b = make_spec("clear.task", name="配置2")
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            fake_runner = SimpleNamespace(
                run=lambda: COAPRunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS)
                )
            )
            factory_calls: list[str] = []

            def factory(spec, manager, episode):
                factory_calls.append(spec.task_id)
                return SimpleNamespace(
                    spec=spec,
                    manager=manager,
                    server=None,
                    agents=[],
                    world_workspace=None,
                    registry_file=Path(tmp) / "chests.json",
                    close_runtime=lambda: None,
                )

            with patch.object(
                COAPRunner,
                "from_bundle",
                return_value=fake_runner,
            ) as from_bundle:
                sequential = COAPSequentialRunner(
                    specs=[spec_a, spec_b],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=COAPConfig(),
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
            self.assertEqual(result.episodes[0].task_id, "clear.task")
            self.assertEqual(result.episodes[1].task_id, "clear.task")
            self.assertEqual(result.episodes[0].episode_id, "1")
            self.assertEqual(result.episodes[1].episode_id, "2")
            self.assertEqual(result.exit_code, 0)

            # recording 层确认 run 目录下按 1/2 编号的 episode 目录
            episode_dirs = sorted(
                p.name for p in recorder.path.iterdir() if p.is_dir()
            )
            self.assertEqual(episode_dirs, ["1", "2"])
            recorder.finalize()

    def test_llm_client_factory_uses_fresh_client_per_episode(self) -> None:
        class StrictClient:
            def __init__(self, name: str) -> None:
                self.name = name
                self.bound = None
                self.closed = False

            def bind_call_recorder(self, recorder) -> None:
                if self.bound is not None:
                    raise RuntimeError("recorder rebound")
                self.bound = recorder

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            clients: list[StrictClient] = []
            seen_clients: list[StrictClient] = []

            def client_factory() -> StrictClient:
                client = StrictClient(f"c{len(clients) + 1}")
                clients.append(client)
                return client

            def from_bundle(bundle, **kwargs):
                seen_clients.append(kwargs["llm_client"])
                return SimpleNamespace(
                    run=lambda: COAPRunResult(
                        outcome=TaskOutcome(TaskStatus.SUCCESS)
                    )
                )

            with patch.object(COAPRunner, "from_bundle", side_effect=from_bundle):
                sequential = COAPSequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client_factory=client_factory,
                    config=COAPConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=lambda spec, manager, episode: SimpleNamespace(
                        spec=spec,
                        manager=manager,
                        server=None,
                        agents=[],
                        world_workspace=None,
                        registry_file=Path(tmp) / "chests.json",
                        close_runtime=lambda: None,
                    ),
                )
                result = sequential.run()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual([client.name for client in clients], ["c1", "c2"])
            self.assertEqual(seen_clients, clients)
            self.assertTrue(all(client.bound is not None for client in clients))
            self.assertTrue(all(client.closed for client in clients))
            recorder.finalize()

    def test_exit_code_nonzero_when_any_episode_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            failed = SimpleNamespace(
                run=lambda: COAPRunResult(
                    outcome=TaskOutcome(TaskStatus.FAILURE)
                )
            )
            with patch.object(
                COAPRunner,
                "from_bundle",
                return_value=failed,
            ) as from_bundle:
                sequential = COAPSequentialRunner(
                    specs=[
                        make_spec("clear.task", name="配置1"),
                        make_spec("clear.task", name="配置2"),
                    ],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=COAPConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=lambda spec, manager, episode: SimpleNamespace(
                        spec=spec,
                        manager=manager,
                        server=None,
                        agents=[],
                        world_workspace=None,
                        registry_file=Path(tmp) / "chests.json",
                        close_runtime=lambda: None,
                    ),
                )
                result = sequential.run()
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(from_bundle.call_count, 2)
            recorder.finalize()

    def test_cleanup_warning_does_not_retry_successful_episode(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            successful = SimpleNamespace(
                run=lambda: COAPRunResult(
                    outcome=TaskOutcome(TaskStatus.SUCCESS),
                    cleanup_errors=[("coap-planning-Alice", "stop timeout")],
                )
            )
            with patch.object(
                COAPRunner,
                "from_bundle",
                return_value=successful,
            ) as from_bundle:
                sequential = COAPSequentialRunner(
                    specs=[make_spec("clear.task")],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=COAPConfig(),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=lambda spec, manager, episode: SimpleNamespace(
                        spec=spec,
                        manager=manager,
                        server=None,
                        agents=[],
                        world_workspace=None,
                        registry_file=Path(tmp) / "chests.json",
                        close_runtime=lambda: None,
                    ),
                )
                result = sequential.run()

            self.assertEqual(from_bundle.call_count, 1)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.process_exit_code, 0)
            self.assertEqual(
                result.episodes[0].result.cleanup_errors,
                [("coap-planning-Alice", "stop timeout")],
            )
            recorder.finalize()

    def test_reuse_previous_loads_previous_episode_shared_cache(self) -> None:
        spec_a = make_spec("clear.task", name="配置1")
        spec_b = make_spec("clear.task", name="配置2")
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            recorder = RecordingManager(
                RecordingConfig(root_dir=tmp)
            ).start_run("coap", task="sequential")
            cache_sources = []

            def factory(spec, manager, episode):
                return SimpleNamespace(
                    spec=spec,
                    manager=manager,
                    server=None,
                    agents=[],
                    world_workspace=None,
                    registry_file=Path(tmp) / "chests.json",
                    close_runtime=lambda: None,
                )

            def write_snapshot(path: Path) -> None:
                cache_dir = path / "cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / "shared.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "coap_shared_cache",
                            "task_id": "clear.task",
                            "sharing": "shared",
                            "exported_at": 1.0,
                            "entry_count": 1,
                            "entries": [
                                {
                                    "entry_id": "entry-1",
                                    "previous_action": {
                                        "name": "wait.wait",
                                        "parameters": {},
                                    },
                                    "next_action": {
                                        "name": "navigation.goto_position",
                                        "parameters": {},
                                    },
                                    "reason": "reuse",
                                    "score": 0.7,
                                    "source": "env",
                                    "hit_count": 0,
                                    "attempt_count": 0,
                                    "success_count": 0,
                                    "delta_attempt_count": 0,
                                    "delta_success_count": 0,
                                    "delta_hit_count": 0,
                                    "task_scope": "clear.task",
                                    "owner_agent_id": None,
                                    "created_at": 1.0,
                                    "updated_at": 1.0,
                                    "last_hit_at": None,
                                    "metadata": {},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            def from_bundle(bundle, **kwargs):
                cache_sources.append(kwargs.get("cache_source"))
                episode = kwargs["episode_recorder"]

                def run():
                    write_snapshot(Path(episode.path))
                    return COAPRunResult(outcome=TaskOutcome(TaskStatus.SUCCESS))

                return SimpleNamespace(run=run)

            with patch.object(COAPRunner, "from_bundle", side_effect=from_bundle):
                sequential = COAPSequentialRunner(
                    specs=[spec_a, spec_b],
                    llm_client=SimpleNamespace(
                        bind_call_recorder=lambda recorder: None
                    ),
                    config=COAPConfig(
                        cache_boot=CacheBootConfig(
                            mode=CacheBootMode.REUSE_PREVIOUS
                        )
                    ),
                    run_recorder=recorder,
                    server_dir=tmp,
                    bridge_port_start=3201,
                    benchmark_root=tmp,
                    bundle_factory=factory,
                )
                result = sequential.run()

            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(cache_sources[0])
            self.assertIsNotNone(cache_sources[1])
            self.assertEqual(cache_sources[1].shared_entries[0].entry_id, "entry-1")
            recorder.finalize()


if __name__ == "__main__":
    unittest.main()
