#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验或运行指定任务环境；不包含任何 Agent 自主策略。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    TaskEnvironmentRunner,
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    TaskWorldWorkspace,
    create_task_environment_bundle,
    create_default_registry,
    expand_task_args,
    load_spec,
    parse_seeded_task_arg,
    resolve_task_world,
    skill_agent_options,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        help="内置 task_id、task@seed 或任务 YAML 路径",
    )
    parser.add_argument(
        "--task-seed",
        type=int,
        help="seed preset 的 episode 1 基准 seed；仅支持 mining/pvp/debug",
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="列出所有内置任务后退出",
    )
    parser.add_argument(
        "--server-dir",
        type=Path,
        default=DEFAULT_SERVER_DIR,
        help="服务器实例目录（默认: src/minecraft）",
    )
    parser.add_argument("--bridge-port-start", type=int, default=3201)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="任务完成、失败或超时后保留现场，等待 Ctrl+C 再清理",
    )
    args = parser.parse_args()

    registry = create_default_registry()
    if args.list_tasks:
        for registered in registry.list():
            print(f"{registered.task_id}\t{registered.category}\t{registered.name}")
        return 0
    if not args.task:
        parser.error("必须提供 --task；可用 --list-tasks 查看内置任务")

    task_path = Path(args.task)
    try:
        if args.task_seed is not None or parse_seeded_task_arg(args.task) is not None:
            spec = expand_task_args([args.task], seed=args.task_seed)[0]
        else:
            spec = load_spec(task_path) if task_path.is_file() else registry.get(args.task)
        manager = registry.manager_for(spec.category)()
        source_world = resolve_task_world(spec, BENCHMARK_ROOT)
        if args.check:
            manager.configure(spec)
    except (TaskRegistryError, TaskSpecError, TaskWorldError) as exc:
        parser.error(str(exc))

    if args.check:
        print(
            f"任务校验通过: {spec.task_id}, category={spec.category}, "
            f"agents={spec.agents['count']}, "
            f"timeout={spec.completion['timeout_seconds']}s, "
            f"world={source_world.name}"
        )
        return 0
    try:
        return _run_environment(args, spec, manager, source_world)
    except TaskWorldError as exc:
        print(f"任务世界准备失败: {exc}", file=sys.stderr)
        return 1


def _run_environment(args, spec, manager, source_world: Path) -> int:
    log_dir = ROOT / "script" / "logs"
    registry_root = ROOT / ".tmp"
    bundle = create_task_environment_bundle(
        spec=spec,
        manager=manager,
        source_world=source_world,
        server_dir=args.server_dir,
        bridge_port_start=args.bridge_port_start,
        log_dir=log_dir,
        runtime_root=registry_root,
    )
    try:
        runner = TaskEnvironmentRunner(
            spec=spec,
            manager=manager,
            server=bundle.server,
            agents=bundle.agents,
            world_workspace=bundle.world_workspace,
            poll_interval=args.poll_interval,
            debug=args.debug,
            metrics_reporter=lambda metrics: print(
                json.dumps(
                    {"metrics": metrics},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            ),
        )
        return runner.run().exit_code
    finally:
        bundle.close_runtime()


def _skill_agent_options(spec, runtime_path: Path, registry_file: Path) -> dict:
    return skill_agent_options(spec, runtime_path, registry_file)


if __name__ == "__main__":
    raise SystemExit(main())
