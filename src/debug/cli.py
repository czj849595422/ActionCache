"""统一交互式 Debug Runner 命令行编排。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Sequence
from uuid import uuid4

from src.tasks import (
    TaskEnvironmentSession,
    TaskRegistryError,
    TaskSpecError,
    TaskWorldError,
    create_default_registry,
    create_task_environment_bundle,
    expand_task_args,
    load_spec,
    parse_seeded_task_arg,
    resolve_task_world,
)

from .controller import DebugController
from .recorder import DebugRecorder
from .repl import DebugRepl
from .schemas import to_jsonable, utc_timestamp
from .task_adapter import TaskDebugAdapter


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
DEFAULT_DEBUG_ROOT = ROOT / ".tmp" / "debug_sessions"
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动一次性任务环境并进入人工调试 REPL，不运行 Agent 自主策略。"
    )
    parser.add_argument("--task", help="内置 task_id、task@seed 或任务 YAML 路径")
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
    parser.add_argument("--max-run-seconds", type=float)
    parser.add_argument("--run-id")
    parser.add_argument("--start-episode", type=int, default=1)
    parser.add_argument("--episode-count", type=int)
    parser.add_argument(
        "--unsafe-actions",
        action="store_true",
        help="开放全部普通 Skill；Manager 内部动作仍保持隐藏",
    )
    parser.add_argument(
        "--debug-root",
        type=Path,
        default=DEFAULT_DEBUG_ROOT,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = create_default_registry()
    if args.list_tasks:
        for spec in registry.list():
            print(f"{spec.task_id}\t{spec.category}\t{spec.name}")
        return 0
    if not args.task:
        parser.error("必须提供 --task；可用 --list-tasks 查看内置任务")
    if args.poll_interval <= 0:
        parser.error("--poll-interval 必须大于 0")
    if args.max_run_seconds is not None and args.max_run_seconds <= 0:
        parser.error("--max-run-seconds 必须为正数")
    if args.start_episode < 1:
        parser.error("--start-episode 必须 >= 1")
    if args.episode_count is not None and args.episode_count < 1:
        parser.error("--episode-count 必须 >= 1")

    if _batch_requested(args):
        try:
            specs = expand_task_args(
                [args.task],
                seed=args.task_seed,
                start_episode=args.start_episode,
                episode_count=args.episode_count,
            )
            return _run_batch_smoke(args, specs, registry)
        except (TaskRegistryError, TaskSpecError, TaskWorldError, ValueError) as exc:
            parser.error(str(exc))

    task_path = Path(args.task)
    try:
        if args.task_seed is not None or parse_seeded_task_arg(args.task) is not None:
            spec = expand_task_args([args.task], seed=args.task_seed)[0]
        else:
            spec = load_spec(task_path) if task_path.is_file() else registry.get(args.task)
        manager = registry.manager_for(spec.category)()
        source_world = resolve_task_world(spec, DEFAULT_BENCHMARK_ROOT)
    except (TaskRegistryError, TaskSpecError, TaskWorldError) as exc:
        parser.error(str(exc))

    recorder = DebugRecorder(
        args.debug_root,
        {
            "task_id": spec.task_id,
            "category": spec.category,
            "agents": list(spec.agents.get("names") or []),
            "server_dir": str(args.server_dir.resolve()),
            "source_world": str(source_world),
            "bridge_port_start": args.bridge_port_start,
            "poll_interval": args.poll_interval,
            "unsafe_actions": bool(args.unsafe_actions),
        },
    )
    print(f"Debug 记录目录: {recorder.session_dir}")
    if args.unsafe_actions:
        print(
            "警告: --unsafe-actions 已开放全部普通 Skill；任务 Manager 内部动作仍不可调用",
            file=sys.stderr,
        )

    bundle = None
    adapter = None
    interrupted = False
    try:
        bundle = create_task_environment_bundle(
            spec=spec,
            manager=manager,
            source_world=source_world,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            log_dir=ROOT / "script" / "logs",
            runtime_root=ROOT / ".tmp",
            unsafe_actions=args.unsafe_actions,
        )
        session = TaskEnvironmentSession(
            spec=spec,
            manager=manager,
            server=bundle.server,
            agents=bundle.agents,
            world_workspace=bundle.world_workspace,
        )
        adapter = TaskDebugAdapter(
            session,
            bundle=bundle,
            recorder=recorder,
            poll_interval=args.poll_interval,
        )
        controller = DebugController(adapter, recorder)
        adapter.start()
        repl = DebugRepl(controller)
        repl.cmdloop()
        recorder.record_event("repl.exit", {"reason": repl.exit_reason or "exit"})
    except KeyboardInterrupt:
        interrupted = True
        print("\n收到 Ctrl+C，开始清理任务现场")
        recorder.record_event("repl.exit", {"reason": "ctrl_c"})
    except Exception as exc:  # noqa: BLE001
        print(f"Debug Runner 启动或运行失败: {exc}", file=sys.stderr)
        recorder.record_event(
            "debug_runner.failed",
            ok=False,
            error={"code": "debug_runner_failed", "message": str(exc)},
        )
        return_code = 1
    else:
        return_code = 0
    finally:
        cleanup_errors: list[tuple[str, str]] = []
        if adapter is not None:
            cleanup_errors.extend(adapter.close())
        elif bundle is not None:
            try:
                bundle.close_runtime()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(("runtime_directory", str(exc)))
        if cleanup_errors:
            print(f"清理存在错误: {cleanup_errors}", file=sys.stderr)
            return_code = 1
    if interrupted:
        return 130
    return return_code


def _batch_requested(args: argparse.Namespace) -> bool:
    return (
        args.max_run_seconds is not None
        or args.run_id is not None
        or args.episode_count is not None
        or args.start_episode != 1
    )


def _run_batch_smoke(args: argparse.Namespace, specs, registry) -> int:
    start = int(args.start_episode)
    count = int(args.episode_count or (len(specs) - start + 1))
    if start > len(specs):
        raise TaskSpecError(f"--start-episode={start} 超出初始化数量 {len(specs)}")
    if start - 1 + count > len(specs):
        raise TaskSpecError(
            f"--start-episode={start} + --episode-count={count} "
            f"超出初始化数量 {len(specs)}"
        )
    selected = list(specs[start - 1 : start - 1 + count])
    run_dir, run_id, manifest = _open_or_create_debug_run(args, selected)
    existing_count = len(manifest.get("episodes") or [])
    if existing_count != start - 1:
        raise TaskSpecError(
            f"debug run 已有 {existing_count} 个 episode，start_episode={start} "
            f"要求已有 {start - 1} 个"
        )

    print(f"Debug batch run: {run_dir}")
    if args.unsafe_actions:
        print(
            "警告: --unsafe-actions 已开放全部普通 Skill；任务 Manager 内部动作仍不可调用",
            file=sys.stderr,
        )
    manifest["resumed_at" if args.run_id else "started_at"] = utc_timestamp()
    manifest["command"] = list(sys.argv[1:])
    _write_debug_run_manifest(run_dir, manifest)

    exit_code = 0
    for index, spec in enumerate(selected):
        episode_number = start + index
        result = _run_smoke_episode(args, registry, spec, run_dir, run_id, episode_number)
        manifest.setdefault("episodes", []).append(result)
        _write_debug_run_manifest(run_dir, manifest)
        if result.get("status") != "completed":
            exit_code = 1
    manifest["finished_at"] = utc_timestamp()
    manifest["status"] = "completed" if exit_code == 0 else "failed"
    _write_debug_run_manifest(run_dir, manifest)
    print(f"Debug batch 完成: run_id={run_id}, status={manifest['status']}")
    return exit_code


def _open_or_create_debug_run(args, selected_specs) -> tuple[Path, str, dict]:
    root = Path(args.debug_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.run_id:
        run_id = str(args.run_id)
        if run_id in {".", ".."} or _SAFE_RUN_ID_RE.fullmatch(run_id) is None:
            raise TaskSpecError(
                "--run-id 只能包含英文字母、数字、点、下划线、连字符或加号"
            )
        run_dir = root / run_id
        manifest_path = run_dir / "run.json"
        if not run_dir.is_dir() or not manifest_path.is_file():
            raise TaskSpecError(f"debug run 不存在或缺少 run.json: {run_dir}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TaskSpecError(f"debug run 清单无法读取: {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise TaskSpecError(f"debug run 清单必须是映射: {manifest_path}")
        return run_dir, run_id, manifest

    run_id = f"{utc_timestamp().replace('+00:00', 'Z').replace(':', '')}-{uuid4().hex[:8]}"
    run_dir = root / run_id
    run_dir.mkdir(parents=False, exist_ok=False)
    first = selected_specs[0]
    manifest = {
        "kind": "debug_batch_run",
        "run_id": run_id,
        "task_id": first.task_id,
        "category": first.category,
        "episodes": [],
    }
    return run_dir, run_id, manifest


def _run_smoke_episode(
    args,
    registry,
    spec,
    run_dir: Path,
    run_id: str,
    episode_number: int,
) -> dict:
    source_world = resolve_task_world(spec, DEFAULT_BENCHMARK_ROOT)
    manager = registry.manager_for(spec.category)()
    recorder = DebugRecorder(
        run_dir,
        {
            "run_id": run_id,
            "episode_id": str(episode_number),
            "task_id": spec.task_id,
            "category": spec.category,
            "seed": (spec.params or {}).get("generation_seed"),
            "agents": list(spec.agents.get("names") or []),
            "server_dir": str(args.server_dir.resolve()),
            "source_world": str(source_world),
            "bridge_port_start": args.bridge_port_start,
            "poll_interval": args.poll_interval,
            "max_run_seconds": args.max_run_seconds,
            "unsafe_actions": bool(args.unsafe_actions),
        },
        session_id=str(episode_number),
    )
    print(
        f"episode {episode_number}: {spec.task_id} "
        f"seed={(spec.params or {}).get('generation_seed')} -> {recorder.session_dir}"
    )
    bundle = None
    adapter = None
    started = time.monotonic()
    status = "completed"
    error: str | None = None
    try:
        bundle = create_task_environment_bundle(
            spec=spec,
            manager=manager,
            source_world=source_world,
            server_dir=args.server_dir,
            bridge_port_start=args.bridge_port_start,
            log_dir=ROOT / "script" / "logs",
            runtime_root=ROOT / ".tmp",
            unsafe_actions=args.unsafe_actions,
        )
        session = TaskEnvironmentSession(
            spec=spec,
            manager=manager,
            server=bundle.server,
            agents=bundle.agents,
            world_workspace=bundle.world_workspace,
        )
        adapter = TaskDebugAdapter(
            session,
            bundle=bundle,
            recorder=recorder,
            poll_interval=args.poll_interval,
        )
        adapter.start()
        started = time.monotonic()
        deadline = started + float(args.max_run_seconds or 30.0)
        while time.monotonic() < deadline:
            if adapter.outcome() is not None:
                break
            time.sleep(min(args.poll_interval, max(0.0, deadline - time.monotonic())))
        final_status = adapter.status()
        recorder.record_event("smoke.status", final_status)
        recorder.record_event("smoke.metrics", adapter.metrics())
        cleanup_errors = adapter.close()
        if cleanup_errors:
            status = "failed"
            error = f"cleanup_errors={cleanup_errors}"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = str(exc)
        recorder.record_event(
            "smoke.failed",
            ok=False,
            error={"code": "smoke_failed", "message": str(exc)},
        )
        if adapter is not None:
            adapter.close()
        elif bundle is not None:
            bundle.close_runtime()
    elapsed = time.monotonic() - started
    result = {
        "episode_id": str(episode_number),
        "task_id": spec.task_id,
        "category": spec.category,
        "seed": (spec.params or {}).get("generation_seed"),
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
        "path": str(recorder.session_dir),
        "error": error,
    }
    recorder.record_event("smoke.result", result, ok=status == "completed")
    print(json.dumps(to_jsonable(result), ensure_ascii=False, sort_keys=True))
    return to_jsonable(result)


def _write_debug_run_manifest(run_dir: Path, manifest: dict) -> None:
    (run_dir / "run.json").write_text(
        json.dumps(to_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
