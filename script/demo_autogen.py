#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the AutoGen-style hierarchical A2A baseline."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.AutoGen import (  # noqa: E402
    AutoGenConfig,
    AutoGenRunner,
    AutoGenSequentialRunner,
)
from src.llm import AgentLLMClientPool, LLMClient, LLMConfig  # noqa: E402
from src.recording import RecordingManager, resolve_recording_config  # noqa: E402
from src.tasks import (  # noqa: E402
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    create_default_registry,
    create_task_environment_bundle,
    expand_task_args,
    resolve_task_world,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        action="append",
        help="Task id, task@seed, task directory, or init YAML path. Can be repeated.",
    )
    parser.add_argument(
        "--task-seed",
        type=int,
        help="Episode-1 base seed for seed presets; supported: mining/pvp/debug.",
    )
    parser.add_argument("--list-tasks", action="store_true")
    parser.add_argument("--server-dir", type=Path, default=DEFAULT_SERVER_DIR)
    parser.add_argument("--bridge-port-start", type=int, default=3201)
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--max-llm-calls", type=int)
    parser.add_argument("--max-llm-calls-per-agent", type=int)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--start-episode", type=int, default=1)
    parser.add_argument("--episode-count", type=int)
    parser.add_argument("--unsafe-actions", action="store_true")
    parser.add_argument("--enable-chat-actions", action="store_true")
    parser.add_argument("--enable-system-messages", action="store_true")
    parser.add_argument("--system-message-interval", type=float, default=30.0)
    parser.add_argument("--default-subtask-max-rounds", type=int, default=5)
    parser.add_argument("--private-thread-window", type=int, default=20)
    parser.add_argument("--public-result-window", type=int, default=20)
    parser.add_argument("--minecraft-say-results", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = create_default_registry()
    if args.list_tasks:
        for registered in registry.list():
            print(f"{registered.task_id}\t{registered.category}\t{registered.name}")
        return 0
    if not args.task:
        parser.error("missing --task; use --list-tasks to inspect built-in tasks")
    if args.start_episode < 1:
        parser.error("--start-episode must be >= 1")
    if args.episode_count is not None and args.episode_count < 1:
        parser.error("--episode-count must be >= 1")
    if args.system_message_interval <= 0:
        parser.error("--system-message-interval must be positive")

    try:
        specs = expand_task_args(
            args.task,
            seed=args.task_seed,
            start_episode=args.start_episode,
            episode_count=args.episode_count,
        )
        _check_sequential_bounds(args, len(specs))
    except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
        parser.error(str(exc))

    if len(specs) > 1 or _resume_requested(args):
        return _run_sequential(args, specs, registry)
    return _run_single(args, specs[0], registry)


def _config(args) -> AutoGenConfig:
    return AutoGenConfig(
        max_run_seconds=args.max_run_seconds,
        max_llm_calls=args.max_llm_calls,
        max_llm_calls_per_agent=args.max_llm_calls_per_agent,
        enable_chat_actions=args.enable_chat_actions,
        system_messages_enabled=args.enable_system_messages,
        system_message_interval=args.system_message_interval,
        default_subtask_max_rounds=args.default_subtask_max_rounds,
        private_thread_window=args.private_thread_window,
        public_result_window=args.public_result_window,
        minecraft_say_results=args.minecraft_say_results,
    )


def _run_single(args, spec, registry) -> int:
    config = _config(args)
    try:
        manager = registry.manager_for(spec.category)()
        source_world = resolve_task_world(spec, BENCHMARK_ROOT)
        recording_config = resolve_recording_config(
            spec.output,
            root_dir=args.record_dir,
            base_dir=ROOT,
        )
        llm_config = LLMConfig.from_env(str(args.env_file))
        recording_config = replace(
            recording_config,
            redact_values=(*recording_config.redact_values, llm_config.api_key),
        )
        run_recorder = RecordingManager(recording_config).start_run(
            "autogen",
            task=spec.task_id,
            metadata={
                "command": list(args.__dict__.items()),
                "task_id": spec.task_id,
                "config": asdict(config),
            },
        )
        episode_recorder = run_recorder.start_episode(
            metadata={
                "task_spec": spec.to_dict(),
                "autogen_config": asdict(config),
                "agents": list(spec.agents.get("names") or []),
                "llm_config": llm_config.safe_dict(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed to initialize run evidence: {exc}", file=sys.stderr)
        return 1

    client: LLMClient | None = None
    bundle = None
    runner = None
    failure: str | None = None
    exit_code = 1
    try:
        client = AgentLLMClientPool(
            llm_config,
            call_recorder=episode_recorder.llm_calls,
        )
        bundle = create_task_environment_bundle(
            spec=spec,
            manager=manager,
            source_world=source_world,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            log_dir=episode_recorder.logs_dir,
            runtime_root=ROOT / ".tmp",
            unsafe_actions=args.unsafe_actions,
        )
        try:
            runner = AutoGenRunner.from_bundle(
                bundle,
                llm_client=client,
                config=config,
                episode_recorder=episode_recorder,
            )
        except BaseException:
            bundle.close_runtime()
            raise
        result = runner.run()
        exit_code = result.exit_code
        if exit_code != 0 and result.stop_reason:
            print(f"AutoGen stop reason: {result.stop_reason}")
    except KeyboardInterrupt:
        failure = "KeyboardInterrupt"
        exit_code = 130
        print("interrupted; saving run evidence", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failure = str(exc)
        print(f"AutoGen run failed: {exc}", file=sys.stderr)
    finally:
        if runner is None and bundle is not None:
            try:
                bundle.close_runtime()
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
                exit_code = 1
        if episode_recorder.status == "started":
            try:
                episode_recorder.finalize(
                    status="interrupted" if exit_code == 130 else "failed",
                    error=failure or "AutoGen runner did not finish",
                )
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
                exit_code = 1
        try:
            run_recorder.finalize(
                status="completed" if episode_recorder.status == "completed" else "failed"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"failed to finalize run evidence: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"run evidence: {episode_recorder.path}")
    return exit_code


def _run_sequential(args, specs, registry) -> int:
    start = args.start_episode
    count = args.episode_count or (len(specs) - start + 1)
    selected = specs[start - 1 : start - 1 + count]
    config = _config(args)
    try:
        recording_config = resolve_recording_config(
            selected[0].output,
            root_dir=args.record_dir,
            base_dir=ROOT,
        )
        llm_config = LLMConfig.from_env(str(args.env_file))
        recording_config = replace(
            recording_config,
            redact_values=(*recording_config.redact_values, llm_config.api_key),
        )
        recording_manager = RecordingManager(recording_config)
        if args.run_id:
            run_recorder = recording_manager.open_run(
                "autogen",
                task=selected[0].task_id,
                run_id=args.run_id,
            )
            config = AutoGenConfig(**run_recorder.config_values())
            run_recorder.update_metadata(
                resumed_at=time.time(),
                command=list(args.__dict__.items()),
                start_episode=args.start_episode,
                episode_count=args.episode_count,
            )
        else:
            run_recorder = recording_manager.start_run(
                "autogen",
                task=selected[0].task_id,
                metadata={
                    "command": list(args.__dict__.items()),
                    "task_ids": [item.task_id for item in selected],
                    "kind": "sequential_configs",
                    "config_count": len(selected),
                    "config": asdict(config),
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"failed to initialize run evidence: {exc}", file=sys.stderr)
        return 1

    exit_code = 1
    try:
        sequential = AutoGenSequentialRunner(
            specs=selected,
            llm_client_factory=lambda: AgentLLMClientPool(llm_config),
            config=config,
            run_recorder=run_recorder,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            benchmark_root=BENCHMARK_ROOT,
            registry=registry,
            unsafe_actions=args.unsafe_actions,
            start_episode=start,
            episode_count=count,
        )
        result = sequential.run()
        exit_code = result.process_exit_code
        print(f"AutoGen sequential run finished: {result.to_dict()}")
    except KeyboardInterrupt:
        exit_code = 130
        print("interrupted; saving run evidence", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"AutoGen sequential run failed: {exc}", file=sys.stderr)
    finally:
        try:
            run_recorder.finalize()
        except Exception as exc:  # noqa: BLE001
            print(f"failed to finalize run evidence: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"run evidence: {run_recorder.path}")
    return exit_code


def _check_sequential_bounds(args, spec_count: int) -> None:
    start = args.start_episode
    if start > spec_count:
        raise TaskSpecError(
            f"--start-episode={start} exceeds config count {spec_count}"
        )
    if args.episode_count is not None and start - 1 + args.episode_count > spec_count:
        raise TaskSpecError(
            f"--start-episode={start} + --episode-count={args.episode_count} "
            f"exceeds config count {spec_count}"
        )


def _resume_requested(args) -> bool:
    return (
        args.run_id is not None
        or args.episode_count is not None
        or args.start_episode != 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
