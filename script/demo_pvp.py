#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行两队 PVP 方法对比任务。"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import AgentLLMClientPool, LLMClient, LLMConfig  # noqa: E402
from src.recording import RecordingManager, resolve_recording_config  # noqa: E402
from src.tasks import (  # noqa: E402
    TaskEnvironmentSession,
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    create_default_registry,
    create_task_environment_bundle,
    expand_task_args,
    parse_seeded_task_arg,
    resolve_task_world,
)
from src.tasks.pvp import (  # noqa: E402
    PVPMatchRunner,
    PVPSequentialRunner,
    PVPTeamRunConfig,
    pvp_method_config,
    pvp_method_names,
    pvp_matchup_id,
    pvp_spec_with_team_size,
    pvp_team_episode_ids,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="pvp")
    parser.add_argument(
        "--task-seed",
        type=int,
        help="PVP seed preset 的 episode 1 基准 seed；也可使用 --task pvp@<seed>",
    )
    parser.add_argument("--list-tasks", action="store_true")
    parser.add_argument(
        "--team-a-method",
        default="basic_async",
        help=f"队伍 A 方法，支持: {', '.join(pvp_method_names())}；别名 ppa/direct/basic 可用",
    )
    parser.add_argument(
        "--team-b-method",
        default="direct_action",
        help=f"队伍 B 方法，支持: {', '.join(pvp_method_names())}；别名 ppa/direct/basic 可用",
    )
    parser.add_argument("--team-size", type=int, default=4)
    parser.add_argument("--server-dir", type=Path, default=DEFAULT_SERVER_DIR)
    parser.add_argument("--bridge-port-start", type=int, default=3201)
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--max-llm-calls", type=int)
    parser.add_argument("--max-llm-calls-per-agent", type=int)
    parser.add_argument("--task-poll-interval", type=float, default=0.2)
    parser.add_argument("--prepare-seconds", type=float, default=10.0)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument(
        "--run-id",
        help="续跑已有 PVP run 的 run_id（不传则新建 run；仅多 episode 顺序运行）",
    )
    parser.add_argument(
        "--start-episode",
        type=int,
        default=1,
        help="从第几个 episode 开始编号（续跑第 x+1 个时传 x+1；仅多 episode 顺序运行）",
    )
    parser.add_argument(
        "--episode-count",
        type=int,
        help="本次运行几个 PVP episode；多轮 seed 会按 episode 递增",
    )
    parser.add_argument("--unsafe-actions", action="store_true")
    parser.add_argument("--enable-chat-actions", action="store_true")
    parser.add_argument("--no-broadcast-results", action="store_true")
    parser.add_argument("--enable-system-messages", action="store_true")
    parser.add_argument("--system-message-interval", type=float, default=30.0)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.team_size < 1:
        parser.error("--team-size 必须 >= 1")
    if args.start_episode < 1:
        parser.error("--start-episode 必须 >= 1")
    if args.episode_count is not None and args.episode_count < 1:
        parser.error("--episode-count 必须 >= 1")
    if args.task_poll_interval <= 0:
        parser.error("--task-poll-interval 必须为正数")
    if args.prepare_seconds < 0:
        parser.error("--prepare-seconds 不能为负")
    if args.system_message_interval <= 0:
        parser.error("--system-message-interval 必须为正数")

    registry = create_default_registry()
    if args.list_tasks:
        for registered in registry.list():
            print(f"{registered.task_id}\t{registered.category}\t{registered.name}")
        return 0

    if _sequential_requested(args):
        try:
            selected_specs = _load_sequential_specs(args)
            worlds = _validate_pvp_specs(selected_specs, registry)
            _validate_pvp_methods(args)
        except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
            parser.error(str(exc))
        if args.check:
            seeds = ", ".join(_spec_seed_text(spec) for spec in selected_specs)
            episode_ids = ", ".join(
                str(args.start_episode + index)
                for index in range(len(selected_specs))
            )
            print(
                f"PVP 多轮校验通过: episodes={len(selected_specs)}, "
                f"episode_ids=[{episode_ids}], seeds=[{seeds}], "
                f"team_size={args.team_size}, worlds={', '.join(sorted(set(worlds)))}, "
                f"methods={args.team_a_method}/{args.team_b_method}"
            )
            return 0
        return _run_sequential(args, selected_specs, registry, argv)

    try:
        spec = _load_single_spec(args.task, registry, seed=args.task_seed)
        if spec.category != "pvp":
            raise TaskSpecError("demo_pvp 仅支持 category=pvp 的任务")
        if args.team_size != int(spec.agents.get("count", 0)) // 2:
            spec = pvp_spec_with_team_size(spec, args.team_size)
        manager = registry.manager_for(spec.category)()
        manager.configure(spec)
        source_world = resolve_task_world(spec, BENCHMARK_ROOT)
    except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
        parser.error(str(exc))

    if args.check:
        print(
            f"PVP 校验通过: {spec.task_id}, agents={spec.agents['count']}, "
            f"team_size={args.team_size}, world={source_world.name}, "
            f"methods={args.team_a_method}/{args.team_b_method}"
        )
        return 0

    try:
        recording_config = resolve_recording_config(
            spec.output,
            root_dir=args.record_dir,
            base_dir=ROOT,
        )
        llm_config = LLMConfig.from_env(str(args.env_file))
        recording_config = replace(
            recording_config,
            redact_values=(
                *recording_config.redact_values,
                llm_config.api_key,
            ),
        )
        matchup_id = pvp_matchup_id(args.team_a_method, args.team_b_method)
        run_recorder = RecordingManager(recording_config).start_run(
            "PVP",
            task=matchup_id,
            metadata={
                "command": list(argv) if argv is not None else sys.argv[1:],
                "task_id": spec.task_id,
                "matchup": matchup_id,
                "team_a_method": args.team_a_method,
                "team_b_method": args.team_b_method,
                "team_size": args.team_size,
                "config": _pvp_runner_config(args),
            },
        )
        episode_recorder = run_recorder.start_episode(
            metadata={
                "task_spec": spec.to_dict(),
                "agents": list(spec.agents.get("names") or []),
                "llm_config": llm_config.safe_dict(),
            }
        )
        team_a_episode_id, team_b_episode_id = pvp_team_episode_ids(
            args.team_a_method,
            args.team_b_method,
        )
        team_a_recorder = episode_recorder.team_episode(
            team_a_episode_id,
            baseline=args.team_a_method,
            metadata={"team_id": "alpha", "method": args.team_a_method},
        )
        team_b_recorder = episode_recorder.team_episode(
            team_b_episode_id,
            baseline=args.team_b_method,
            metadata={"team_id": "bravo", "method": args.team_b_method},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"无法初始化运行记录: {exc}", file=sys.stderr)
        return 1

    client_a: LLMClient | None = None
    client_b: LLMClient | None = None
    bundle = None
    match = None
    exit_code = 1
    failure: str | None = None
    try:
        client_a = AgentLLMClientPool(
            llm_config,
            call_recorder=team_a_recorder.llm_calls,
        )
        client_b = AgentLLMClientPool(
            llm_config,
            call_recorder=team_b_recorder.llm_calls,
        )
        config_a = pvp_method_config(
            args.team_a_method,
            max_run_seconds=args.max_run_seconds,
            max_llm_calls=args.max_llm_calls,
            max_llm_calls_per_agent=args.max_llm_calls_per_agent,
            enable_chat_actions=args.enable_chat_actions,
            broadcast_results=not args.no_broadcast_results,
            system_messages_enabled=args.enable_system_messages,
            system_message_interval=args.system_message_interval,
            task_poll_interval=args.task_poll_interval,
        )
        config_b = pvp_method_config(
            args.team_b_method,
            max_run_seconds=args.max_run_seconds,
            max_llm_calls=args.max_llm_calls,
            max_llm_calls_per_agent=args.max_llm_calls_per_agent,
            enable_chat_actions=args.enable_chat_actions,
            broadcast_results=not args.no_broadcast_results,
            system_messages_enabled=args.enable_system_messages,
            system_message_interval=args.system_message_interval,
            task_poll_interval=args.task_poll_interval,
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
        session = TaskEnvironmentSession(
            spec=bundle.spec,
            manager=bundle.manager,
            server=bundle.server,
            agents=bundle.agents,
            world_workspace=bundle.world_workspace,
            prepare_seconds=args.prepare_seconds,
            output=print,
        )
        match = PVPMatchRunner(
            session=session,
            teams=(
                PVPTeamRunConfig(
                    team_id="alpha",
                    method=args.team_a_method,
                    llm_client=client_a,
                    config=config_a,
                    episode_recorder=team_a_recorder,
                ),
                PVPTeamRunConfig(
                    team_id="bravo",
                    method=args.team_b_method,
                    llm_client=client_b,
                    config=config_b,
                    episode_recorder=team_b_recorder,
                ),
            ),
            registry_path=str(bundle.registry_file),
            episode_recorder=episode_recorder,
            max_run_seconds=args.max_run_seconds,
            task_poll_interval=args.task_poll_interval,
            output=print,
        )
        result = match.run()
        exit_code = result.exit_code
        print(f"PVP 对战完成: {result.to_dict()}")
    except KeyboardInterrupt:
        failure = "KeyboardInterrupt"
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failure = str(exc)
        print(f"PVP 运行失败: {exc}", file=sys.stderr)
    finally:
        if match is None and bundle is not None:
            try:
                bundle.close_runtime()
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
        elif bundle is not None:
            try:
                bundle.close_runtime()
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
                exit_code = 1
        for client in (client_a, client_b):
            if client is None:
                continue
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                failure = failure or str(exc)
                exit_code = 1
        for recorder in (team_a_recorder, team_b_recorder, episode_recorder):
            if recorder.status == "started":
                try:
                    recorder.finalize(
                        status="interrupted" if exit_code == 130 else "failed",
                        error=failure or "PVP runner did not produce a terminal result",
                    )
                except Exception as exc:  # noqa: BLE001
                    failure = failure or str(exc)
                    exit_code = 1
        try:
            run_recorder.finalize(
                status="completed" if episode_recorder.status == "completed" else "failed"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"运行记录终结失败: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"运行证据: {episode_recorder.path}")
    return exit_code


def _sequential_requested(args) -> bool:
    return (
        args.run_id is not None
        or args.episode_count is not None
        or args.start_episode != 1
    )


def _load_sequential_specs(args) -> list:
    seed = _sequential_seed(args)
    specs = expand_task_args(
        [args.task],
        seed=seed,
        start_episode=args.start_episode,
        episode_count=args.episode_count,
    )
    _check_sequential_bounds(args, len(specs))
    start = args.start_episode
    count = args.episode_count or (len(specs) - start + 1)
    selected = specs[start - 1 : start - 1 + count]
    return [_pvp_spec_for_team_size(spec, args.team_size) for spec in selected]


def _sequential_seed(args) -> int | None:
    if args.task_seed is not None:
        return args.task_seed
    if parse_seeded_task_arg(args.task) is not None:
        return None
    if str(args.task).strip() == "pvp":
        return 0
    return None


def _check_sequential_bounds(args, spec_count: int) -> None:
    start = args.start_episode
    if start > spec_count:
        raise TaskSpecError(
            f"--start-episode={start} 超出初始化文件数量 {spec_count}"
        )
    if args.episode_count is not None and start - 1 + args.episode_count > spec_count:
        raise TaskSpecError(
            f"--start-episode={start} + --episode-count={args.episode_count} "
            f"超出初始化文件数量 {spec_count}"
        )


def _pvp_spec_for_team_size(spec, team_size: int):
    if spec.category != "pvp":
        raise TaskSpecError("demo_pvp 仅支持 category=pvp 的任务")
    if team_size != int(spec.agents.get("count", 0)) // 2:
        return pvp_spec_with_team_size(spec, team_size)
    return spec


def _validate_pvp_specs(specs, registry) -> list[str]:
    worlds: list[str] = []
    for spec in specs:
        if spec.category != "pvp":
            raise TaskSpecError("demo_pvp 仅支持 category=pvp 的任务")
        manager = registry.manager_for(spec.category)()
        manager.configure(spec)
        source_world = resolve_task_world(spec, BENCHMARK_ROOT)
        worlds.append(source_world.name)
    return worlds


def _validate_pvp_methods(args) -> None:
    pvp_method_config(args.team_a_method)
    pvp_method_config(args.team_b_method)


def _spec_seed_text(spec) -> str:
    seed = (spec.params or {}).get("generation_seed")
    return str(seed) if seed is not None else "-"


def _pvp_runner_config(args) -> dict[str, object]:
    return {
        "team_a_method": args.team_a_method,
        "team_b_method": args.team_b_method,
        "team_size": args.team_size,
        "max_run_seconds": args.max_run_seconds,
        "max_llm_calls": args.max_llm_calls,
        "max_llm_calls_per_agent": args.max_llm_calls_per_agent,
        "task_poll_interval": args.task_poll_interval,
        "prepare_seconds": args.prepare_seconds,
        "unsafe_actions": args.unsafe_actions,
        "enable_chat_actions": args.enable_chat_actions,
        "broadcast_results": not args.no_broadcast_results,
        "system_messages_enabled": args.enable_system_messages,
        "system_message_interval": args.system_message_interval,
    }


def _run_sequential(args, selected_specs, registry, argv: list[str] | None) -> int:
    recording_config = resolve_recording_config(
        selected_specs[0].output,
        root_dir=args.record_dir,
        base_dir=ROOT,
    )
    matchup_id = pvp_matchup_id(args.team_a_method, args.team_b_method)
    runner_config = _pvp_runner_config(args)
    try:
        llm_config = LLMConfig.from_env(str(args.env_file))
        recording_config = replace(
            recording_config,
            redact_values=(
                *recording_config.redact_values,
                llm_config.api_key,
            ),
        )
        recording_manager = RecordingManager(recording_config)
        metadata = {
            "command": list(argv) if argv is not None else sys.argv[1:],
            "task_ids": [spec.task_id for spec in selected_specs],
            "kind": "pvp_sequential",
            "config_count": len(selected_specs),
            "match_count": len(selected_specs),
            "matchup": matchup_id,
            "team_a_method": args.team_a_method,
            "team_b_method": args.team_b_method,
            "team_size": args.team_size,
            "start_episode": args.start_episode,
            "episode_count": len(selected_specs),
            "seeds": [
                (spec.params or {}).get("generation_seed")
                for spec in selected_specs
            ],
            "config": runner_config,
        }
        if args.run_id:
            run_recorder = recording_manager.open_run(
                "PVP",
                task=matchup_id,
                run_id=args.run_id,
            )
            runner_config = run_recorder.config_values()
            metadata["config"] = runner_config
            for key in _pvp_runner_config(args):
                if key in runner_config:
                    setattr(args, key, runner_config[key])
            metadata.update(
                {
                    "matchup": pvp_matchup_id(
                        args.team_a_method,
                        args.team_b_method,
                    ),
                    "team_a_method": args.team_a_method,
                    "team_b_method": args.team_b_method,
                    "team_size": args.team_size,
                }
            )
            run_recorder.update_metadata(
                resumed_at=time.time(),
                **metadata,
            )
            print(
                f"续跑 PVP run: {run_recorder.path} "
                f"（已有 {run_recorder.episode_count} 个 episode）"
            )
        else:
            run_recorder = recording_manager.start_run(
                "PVP",
                task=matchup_id,
                metadata=metadata,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"无法初始化运行记录: {exc}", file=sys.stderr)
        return 1

    exit_code = 1
    try:
        sequential = PVPSequentialRunner(
            specs=selected_specs,
            team_a_method=args.team_a_method,
            team_b_method=args.team_b_method,
            llm_client_factory=lambda recorder: AgentLLMClientPool(
                llm_config,
                call_recorder=recorder,
            ),
            run_recorder=run_recorder,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            benchmark_root=BENCHMARK_ROOT,
            registry=registry,
            team_size=args.team_size,
            max_run_seconds=args.max_run_seconds,
            max_llm_calls=args.max_llm_calls,
            max_llm_calls_per_agent=args.max_llm_calls_per_agent,
            task_poll_interval=args.task_poll_interval,
            prepare_seconds=args.prepare_seconds,
            unsafe_actions=args.unsafe_actions,
            enable_chat_actions=args.enable_chat_actions,
            broadcast_results=not args.no_broadcast_results,
            system_messages_enabled=args.enable_system_messages,
            system_message_interval=args.system_message_interval,
            llm_config_metadata=llm_config.safe_dict(),
            start_episode=args.start_episode,
            episode_count=len(selected_specs),
            output=print,
        )
        result = sequential.run()
        exit_code = result.exit_code
        print(f"PVP 多轮运行完成: {result.to_dict()}")
    except KeyboardInterrupt:
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"PVP 多轮运行失败: {exc}", file=sys.stderr)
    finally:
        try:
            run_recorder.finalize()
        except Exception as exc:  # noqa: BLE001
            print(f"运行记录终结失败: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"运行证据: {run_recorder.path}")
    return exit_code


def _load_single_spec(task: str, registry, *, seed: int | None = None):
    path = Path(task)
    if seed is not None:
        specs = expand_task_args([task], seed=seed)
    elif path.suffix.lower() in {".yaml", ".yml"} and path.is_file():
        specs = expand_task_args([task])
    else:
        try:
            return registry.get(task)
        except TaskRegistryError:
            specs = expand_task_args([task])
    if len(specs) != 1:
        raise TaskSpecError("demo_pvp 只支持单个 PVP spec")
    return specs[0]


if __name__ == "__main__":
    raise SystemExit(main())
