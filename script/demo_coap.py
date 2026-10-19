#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验或运行 COAP（CACHE-OPTIMIZED ASYNCHRONOUS PLANNING）baseline。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
_COAP_ABLATION_SUFFIXES = (
    ("cache_planning_enabled", "_woCP"),
    ("standard_verification_enabled", "_woSP"),
    ("timed_planning_enabled", "_woPP"),
    ("cache_reasoning_optimization_enabled", "_woRO"),
    ("score_update_enabled", "_woSU"),
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SimpleInstructionRegistry  # noqa: E402
from src.baseline.COAP import (  # noqa: E402
    CacheBootConfig,
    CacheBootMode,
    COAPConfig,
    COAPRunner,
    COAPSequentialRunner,
)
from src.baseline.COAP.adapters import render_catalog  # noqa: E402
from src.llm import AgentLLMClientPool, LLMClient, LLMConfig  # noqa: E402
from src.recording import (  # noqa: E402
    RecordingManager,
    resolve_recording_config,
)
from src.tasks import (  # noqa: E402
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    create_default_registry,
    create_task_environment_bundle,
    expand_task_args,
    resolve_task_world,
    task_action_space,
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
    parser.add_argument("--planning-interval", type=float)
    parser.add_argument("--acting-poll-interval", type=float, default=0.2)
    parser.add_argument("--task-poll-interval", type=float, default=0.2)
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--max-llm-calls", type=int)
    parser.add_argument("--max-llm-calls-per-agent", type=int)
    parser.add_argument(
        "--disable-timed-planning",
        action="store_true",
        help="关闭动作执行期间的定时普通规划",
    )
    parser.add_argument(
        "--disable-cache-planning",
        action="store_true",
        help="关闭双路竞速中的缓存规划",
    )
    parser.add_argument(
        "--disable-standard-verification",
        action="store_true",
        help="关闭动作结束后的普通规划验证；同时关闭定时普通规划",
    )
    parser.add_argument(
        "--disable-cache-reasoning-optimization",
        action="store_true",
        help="关闭缓存候选的内容推理更新",
    )
    parser.add_argument(
        "--disable-score-update",
        action="store_true",
        help="关闭缓存评分更新",
    )
    parser.add_argument(
        "--cache-boot-mode",
        choices=["llm_init", "reuse_previous", "empty"],
        help="缓存初始化模式；默认 llm_init",
    )
    parser.add_argument("--cache-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--enable-chat-actions", action="store_true")
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
    parser.add_argument(
        "--episode-retries",
        type=int,
        default=1,
        help="episode 出现 runner/Worker/清理错误时额外重试次数，默认 1；0 表示关闭",
    )
    parser.add_argument("--unsafe-actions", action="store_true")
    parser.add_argument("--no-broadcast-results", action="store_true")
    parser.add_argument("--enable-system-messages", action="store_true")
    parser.add_argument("--system-message-interval", type=float, default=30.0)
    parser.add_argument("--check", action="store_true")
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
    if args.planning_interval is not None and args.planning_interval <= 0:
        parser.error("--planning-interval 必须为正数")
    if args.system_message_interval <= 0:
        parser.error("--system-message-interval 必须为正数")
    if args.start_episode < 1:
        parser.error("--start-episode 必须 >= 1")
    if args.episode_count is not None and args.episode_count < 1:
        parser.error("--episode-count 必须 >= 1")
    if args.episode_retries < 0:
        parser.error("--episode-retries 必须 >= 0")

    try:
        specs = expand_task_args(
            args.task,
            seed=args.task_seed,
            start_episode=args.start_episode,
            episode_count=args.episode_count,
        )
        spec = specs[0]
        config = _build_coap_config(args, parser)
        recording_config = resolve_recording_config(
            spec.output,
            root_dir=args.record_dir,
            base_dir=ROOT,
        )
        recording_config, ablation_suffix = _with_coap_ablation_suffix(
            recording_config,
            args,
            config,
        )
        manager = registry.manager_for(spec.category)()
        source_world = resolve_task_world(spec, BENCHMARK_ROOT)
        _check_sequential_bounds(args, len(specs))
        if args.check:
            manager.configure(spec)
            _check_catalog(spec, args.unsafe_actions, args.enable_chat_actions)
    except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
        parser.error(str(exc))

    if args.check:
        print(
            f"COAP 校验通过: {spec.task_id}, category={spec.category}, "
            f"agents={spec.agents['count']}, world={source_world.name}"
        )
        return 0

    if len(specs) > 1 or _resume_requested(args):
        return _run_sequential(args, specs, registry, parser)

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
            "coap",
            task=spec.task_id,
            metadata={
                "command": list(argv) if argv is not None else sys.argv[1:],
                "task_id": spec.task_id,
                "config": asdict(config),
                "ablation_suffix": ablation_suffix or None,
            },
        )
        episode_recorder = run_recorder.start_episode(
            metadata={
                "task_spec": spec.to_dict(),
                "coap_config": asdict(config),
                "ablation_suffix": ablation_suffix or None,
                "agents": list(spec.agents.get("names") or []),
                "llm_config": llm_config.safe_dict(),
            }
        )
    except Exception as exc:  # noqa: BLE001
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
            runner = COAPRunner.from_bundle(
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
            print(f"COAP 停止原因: {result.stop_reason}")
        print(f"COAP 缓存统计: {result.cache_stats}")
    except TaskWorldError as exc:
        failure = str(exc)
        print(f"任务世界准备失败: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        failure = "KeyboardInterrupt"
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failure = str(exc)
        print(f"COAP 运行失败: {exc}", file=sys.stderr)
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
                    error=failure or "COAP runner did not produce a terminal result",
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


def _build_cache_boot(args, parser: argparse.ArgumentParser) -> CacheBootConfig:
    if args.cache_source is not None:
        parser.error("--cache-source 已废弃；请使用 --cache-boot-mode reuse_previous")
    if args.cache_boot_mode is None:
        return CacheBootConfig()
    return CacheBootConfig(mode=CacheBootMode(args.cache_boot_mode))


def _build_coap_config(args, parser: argparse.ArgumentParser) -> COAPConfig:
    return COAPConfig(
        cache_planning_enabled=not args.disable_cache_planning,
        planning_interval=(
            5.0 if args.planning_interval is None else args.planning_interval
        ),
        timed_planning_enabled=not args.disable_timed_planning,
        standard_verification_enabled=not args.disable_standard_verification,
        cache_reasoning_optimization_enabled=(
            not args.disable_cache_reasoning_optimization
        ),
        score_update_enabled=not args.disable_score_update,
        acting_poll_interval=args.acting_poll_interval,
        task_poll_interval=args.task_poll_interval,
        max_run_seconds=args.max_run_seconds,
        max_llm_calls=args.max_llm_calls,
        max_llm_calls_per_agent=args.max_llm_calls_per_agent,
        broadcast_results=not args.no_broadcast_results,
        system_messages_enabled=args.enable_system_messages,
        system_message_interval=args.system_message_interval,
        enable_chat_actions=args.enable_chat_actions,
        cache_boot=_build_cache_boot(args, parser),
    )


def _restore_coap_config(values: dict) -> COAPConfig:
    defaults = COAPConfig()
    nested_names = (
        "cache_boot",
        "cache_buffer",
        "cache_retrieval",
        "cache_score",
        "cache_representation",
    )
    updates = {
        key: value
        for key, value in values.items()
        if key not in nested_names
    }
    for key in nested_names:
        value = values.get(key)
        if isinstance(value, dict):
            updates[key] = type(getattr(defaults, key))(**value)
    return replace(defaults, **updates)


def _coap_ablation_suffix(args, config: COAPConfig) -> str:
    """Return a stable suffix describing the explicitly selected ablations."""
    suffixes = []
    for field, suffix in _COAP_ABLATION_SUFFIXES:
        if field == "standard_verification_enabled":
            selected = args.disable_standard_verification
        elif field == "timed_planning_enabled":
            selected = args.disable_timed_planning
        else:
            selected = not getattr(config, field)
        if selected:
            suffixes.append(suffix)
    if config.cache_boot.resolved_mode is CacheBootMode.REUSE_PREVIOUS:
        suffixes.append("_reuse")
    elif config.cache_boot.resolved_mode is CacheBootMode.EMPTY:
        suffixes.append("_woInit")
    return "".join(suffixes)


def _with_coap_ablation_suffix(recording_config, args, config: COAPConfig):
    suffix = _coap_ablation_suffix(args, config)
    if not suffix:
        return recording_config, suffix
    root_dir = recording_config.root_dir
    if not root_dir.name:
        raise ValueError(f"recording root 必须包含可追加后缀的目录名: {root_dir}")
    return replace(
        recording_config,
        root_dir=root_dir.parent / f"{root_dir.name}{suffix}",
    ), suffix


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


def _run_sequential(args, specs, registry, parser: argparse.ArgumentParser) -> int:
    """按序运行任务初始化文件（config/ 编号展开结果），每个文件一个 episode。"""
    start = args.start_episode
    count = args.episode_count or (len(specs) - start + 1)
    selected = specs[start - 1 : start - 1 + count]
    config = _build_coap_config(args, parser)
    recording_config = resolve_recording_config(
        selected[0].output,
        root_dir=args.record_dir,
        base_dir=ROOT,
    )
    recording_config, ablation_suffix = _with_coap_ablation_suffix(
        recording_config,
        args,
        config,
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
                "coap",
                task=selected[0].task_id,
                run_id=args.run_id,
            )
            config = _restore_coap_config(run_recorder.config_values())
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
                "coap",
                task=selected[0].task_id,
                metadata={
                    "command": list(args.__dict__.items()),
                    "task_ids": [item.task_id for item in selected],
                    "kind": "sequential_configs",
                    "config_count": len(selected),
                    "config": asdict(config),
                    "ablation_suffix": ablation_suffix or None,
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"无法初始化运行记录: {exc}", file=sys.stderr)
        return 1
    exit_code = 1
    try:
        sequential = COAPSequentialRunner(
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
            episode_retries=args.episode_retries,
        )
        result = sequential.run()
        exit_code = result.process_exit_code
        print(f"COAP 顺序运行完成: {result.to_dict()}")
    except KeyboardInterrupt:
        exit_code = 130
        print("收到 Ctrl+C，开始保存运行证据", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"COAP 顺序运行失败: {exc}", file=sys.stderr)
    finally:
        try:
            run_recorder.finalize()
        except Exception as exc:  # noqa: BLE001
            print(f"运行记录终结失败: {exc}", file=sys.stderr)
            exit_code = 1
        print(f"运行证据: {run_recorder.path}")
    return exit_code


def _check_catalog(spec, unsafe_actions: bool, enable_chat_actions: bool) -> None:
    from src.tasks import all_normal_skill_actions, task_internal_actions

    internal_actions = task_internal_actions(spec)
    allowed = (
        all_normal_skill_actions(internal_actions=internal_actions)
        if unsafe_actions
        else task_action_space(spec)
    )
    registry = _skill_registry(allowed)
    instructions = SimpleInstructionRegistry()
    catalog = render_catalog(
        registry,
        instructions,
        "zh",
        enable_chat_actions=enable_chat_actions,
    )
    if not catalog.strip():
        raise ValueError("技能目录中没有当前任务可用的 action")


def _skill_registry(allowed):
    from src.agents.action import SkillRegistry

    return SkillRegistry(allowed_actions=allowed)


if __name__ == "__main__":
    raise SystemExit(main())
