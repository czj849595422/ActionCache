#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验或运行 Parallelized Planning-Acting baseline。"""

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

from src.agents.action import (  # noqa: E402
    ActionMode,
    SimpleInstructionRegistry,
    SkillRegistry,
)
from src.baseline.Parallelized import (  # noqa: E402
    JsonDecisionCodec,
    PPAConfig,
    PPARunner,
    PPASequentialRunner,
)
from src.baseline.Parallelized.preflight import run_preflight  # noqa: E402
from src.llm import AgentLLMClientPool, LLMClient, LLMConfig  # noqa: E402
from src.recording import (  # noqa: E402
    RecordingManager,
    resolve_recording_config,
)
from src.tasks import (  # noqa: E402
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    all_normal_skill_actions,
    create_default_registry,
    create_task_environment_bundle,
    expand_task_args,
    resolve_task_world,
    task_action_space,
    task_internal_actions,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        action="append",
        help=(
            "任务名（内置 task_id 或任务文件夹路径，自动读取 config/ 编号初始化文件）"
            "、task@seed，或初始化 YAML 路径（可多次）"
        ),
    )
    parser.add_argument(
        "--task-seed",
        type=int,
        help="seed preset 的 episode 1 基准 seed；仅支持 mining/pvp/debug",
    )
    parser.add_argument("--list-tasks", action="store_true")
    parser.add_argument("--server-dir", type=Path, default=DEFAULT_SERVER_DIR)
    parser.add_argument("--bridge-port-start", type=int, default=3201)
    parser.add_argument("--observation-interval", type=float, default=1.0)
    parser.add_argument("--acting-poll-interval", type=float, default=0.2)
    parser.add_argument("--task-poll-interval", type=float, default=0.2)
    parser.add_argument("--planning-interval", type=float)
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--max-llm-calls", type=int)
    parser.add_argument("--max-llm-calls-per-agent", type=int)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument(
        "--run-id",
        help="续跑已有 run 的 run_id（不传则新建 run；仅多任务顺序运行）",
    )
    parser.add_argument(
        "--start-episode",
        type=int,
        default=1,
        help="从第几个 episode 开始编号（续跑第 x+1 个时传 x+1；仅多任务顺序运行）",
    )
    parser.add_argument(
        "--episode-count",
        type=int,
        help="本次运行几个 episode，默认跑完所有 --task（仅多任务顺序运行）",
    )
    parser.add_argument("--unsafe-actions", action="store_true")
    parser.add_argument("--no-broadcast-results", action="store_true")
    parser.add_argument("--enable-system-messages", action="store_true")
    parser.add_argument("--system-message-interval", type=float, default=30.0)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preflight", action="store_true")
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
        parser.error("必须提供 --task；可用 --list-tasks 查看内置任务")
    if args.planning_interval is not None and args.planning_interval < 0:
        parser.error("--planning-interval 不能为负")
    if args.start_episode < 1:
        parser.error("--start-episode 必须 >= 1")
    if args.episode_count is not None and args.episode_count < 1:
        parser.error("--episode-count 必须 >= 1")
    if args.max_run_seconds is not None and args.max_run_seconds <= 0:
        parser.error("--max-run-seconds 必须为正数")
    if args.system_message_interval <= 0:
        parser.error("--system-message-interval 必须为正数")
    if args.max_llm_calls is not None and args.max_llm_calls < 1:
        parser.error("--max-llm-calls 必须 >= 1")
    if (
        args.max_llm_calls_per_agent is not None
        and args.max_llm_calls_per_agent < 1
    ):
        parser.error("--max-llm-calls-per-agent 必须 >= 1")
    if args.check and args.preflight:
        parser.error("--check 与 --preflight 不能同时使用")

    try:
        specs = expand_task_args(
            args.task,
            seed=args.task_seed,
            start_episode=args.start_episode,
            episode_count=args.episode_count,
        )
        spec = specs[0]
        recording_config = resolve_recording_config(
            spec.output,
            root_dir=args.record_dir,
            base_dir=ROOT,
        )
        manager = registry.manager_for(spec.category)()
        if args.check or not args.preflight:
            source_world = resolve_task_world(spec, BENCHMARK_ROOT)
        _check_sequential_bounds(args, len(specs))
        if (args.check or args.preflight) and (
            len(specs) > 1 or _resume_requested(args)
        ):
            raise TaskSpecError("--check/--preflight 仅支持单个初始化文件")
        if args.check:
            manager.configure(spec)
            _check_protocol_and_skills(spec, args.unsafe_actions)
            SimpleInstructionRegistry()
    except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
        parser.error(str(exc))

    if args.check:
        print(
            f"PPA 校验通过: {spec.task_id}, category={spec.category}, "
            f"agents={spec.agents['count']}, world={source_world.name}, "
            f"record_dir={recording_config.root_dir.resolve()}"
        )
        return 0

    if args.preflight:
        report = run_preflight(
            spec=spec,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            benchmark_root=BENCHMARK_ROOT,
            env_file=args.env_file,
            record_dir=args.record_dir,
            output=print,
        )
        report.print_report(output=print)
        return report.exit_code

    if len(specs) > 1 or _resume_requested(args):
        return _run_sequential(args, specs, registry)

    config = PPAConfig(
        observation_interval=args.observation_interval,
        acting_poll_interval=args.acting_poll_interval,
        task_poll_interval=args.task_poll_interval,
        planning_interval=(
            0.0 if args.planning_interval is None else args.planning_interval
        ),
        max_run_seconds=args.max_run_seconds,
        max_llm_calls=args.max_llm_calls,
        max_llm_calls_per_agent=args.max_llm_calls_per_agent,
        broadcast_results=not args.no_broadcast_results,
        system_messages_enabled=args.enable_system_messages,
        system_message_interval=args.system_message_interval,
    )
    try:
        llm_config = LLMConfig.from_env(str(args.env_file))
        recording_config = replace(
            recording_config,
            redact_values=(
                *recording_config.redact_values,
                llm_config.api_key,
            ),
        )
        run_recorder = RecordingManager(recording_config).start_run(
            "parallelized",
            task=spec.task_id,
            metadata={
                "command": list(argv) if argv is not None else sys.argv[1:],
                "task_id": spec.task_id,
                "config": asdict(config),
            },
        )
        episode_recorder = run_recorder.start_episode(
            metadata={
                "task_spec": spec.to_dict(),
                "ppa_config": asdict(config),
                "agents": list(spec.agents.get("names") or []),
                "llm_config": llm_config.safe_dict(),
            }
        )
    except Exception as exc:  # configuration and recorder initialization
        print(f"无法初始化运行记录: {exc}", file=sys.stderr)
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
            runner = PPARunner.from_bundle(
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
            print(f"PPA 停止原因: {result.stop_reason}")
    except TaskWorldError as exc:
        failure = str(exc)
        print(f"任务世界准备失败: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        failure = "KeyboardInterrupt"
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failure = str(exc)
        print(f"PPA 运行失败: {exc}", file=sys.stderr)
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
                    error=failure or "PPA runner did not produce a terminal result",
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


def _check_sequential_bounds(args, spec_count: int) -> None:
    """校验起始编号/数量在 config 初始化文件范围内（供 parser.error 使用）。"""
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


def _resume_requested(args) -> bool:
    return (
        args.run_id is not None
        or args.episode_count is not None
        or args.start_episode != 1
    )


def _run_sequential(args, specs, registry) -> int:
    """按序运行任务初始化文件（config/ 编号展开结果），每个文件一个 episode。"""
    start = args.start_episode
    count = args.episode_count or (len(specs) - start + 1)
    selected = specs[start - 1 : start - 1 + count]
    config = PPAConfig(
        observation_interval=args.observation_interval,
        acting_poll_interval=args.acting_poll_interval,
        task_poll_interval=args.task_poll_interval,
        planning_interval=(
            0.0 if args.planning_interval is None else args.planning_interval
        ),
        max_run_seconds=args.max_run_seconds,
        max_llm_calls=args.max_llm_calls,
        max_llm_calls_per_agent=args.max_llm_calls_per_agent,
        broadcast_results=not args.no_broadcast_results,
        system_messages_enabled=args.enable_system_messages,
        system_message_interval=args.system_message_interval,
    )
    recording_config = resolve_recording_config(
        selected[0].output,
        root_dir=args.record_dir,
        base_dir=ROOT,
    )
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
        if args.run_id:
            run_recorder = recording_manager.open_run(
                "parallelized",
                task=selected[0].task_id,
                run_id=args.run_id,
            )
            config = PPAConfig(**run_recorder.config_values())
            run_recorder.update_metadata(
                resumed_at=time.time(),
                command=list(args.__dict__.items()),
                start_episode=args.start_episode,
                episode_count=args.episode_count,
            )
            print(
                f"续跑 run: {run_recorder.path} "
                f"（已有 {run_recorder.episode_count} 个 episode）"
            )
        else:
            run_recorder = recording_manager.start_run(
                "parallelized",
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
        print(f"无法初始化运行记录: {exc}", file=sys.stderr)
        return 1
    exit_code = 1
    try:
        sequential = PPASequentialRunner(
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
        print(f"PPA 顺序运行完成: {result.to_dict()}")
    except KeyboardInterrupt:
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"PPA 顺序运行失败: {exc}", file=sys.stderr)
    finally:
        try:
            run_recorder.finalize()
        except Exception as exc:  # noqa: BLE001
            print(f"运行记录终结失败: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"运行证据: {run_recorder.path}")
    return exit_code


def _check_protocol_and_skills(spec, unsafe_actions: bool) -> None:
    internal_actions = task_internal_actions(spec)
    if unsafe_actions:
        allowed_actions = all_normal_skill_actions(
            internal_actions=internal_actions,
        )
    else:
        allowed_actions = task_action_space(spec)
    registry = SkillRegistry(allowed_actions=allowed_actions)
    actions = sorted(
        f"{skill_name}.{command_name}"
        for skill_name, manifest in registry.manifests.items()
        for command_name in manifest.commands
        if registry.is_allowed(f"{skill_name}.{command_name}")
    )
    if not actions:
        raise ValueError("技能目录中没有当前任务可用的 action")
    if "wait.wait" not in actions:
        raise ValueError("当前任务未启用协议自检所需的 wait.wait action")
    codec = JsonDecisionCodec()
    proposal = codec.parse(
        '{"skill": "wait.wait(seconds=0)", "interrupt": false, '
        '"reason": "protocol check"}',
        agent_id="check-agent",
        registry=registry,
    )
    if proposal.action_unit.mode != ActionMode.CONTINUOUS:
        raise ValueError("JSON 协议未优先选择 continuous mode")


if __name__ == "__main__":
    raise SystemExit(main())
