#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量指令简易 runner: 两阶段 (准备 + 任务) 执行测试。

流程:
  1. 解析指令计划 (准备阶段 + 任务阶段);
  2. 启动 Minecraft 服务器, 立即按顺序执行准备阶段环境指令;
  3. 等待任务开始信号: --start-delay N 自动触发, 否则按回车触发;
  4. 收到信号后开始计时, 按时间顺序执行任务阶段指令;
  5. 清理: disconnect 不做 (本 runner 无 agent), 优雅停止服务器。

用法:
  python script/demo_batch_runner.py --file script/batch_runner_example.txt --start-delay 5
  python script/demo_batch_runner.py --file x.txt                # 按回车触发开始信号
  python script/demo_batch_runner.py --file x.txt --check       # 仅解析校验
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.launcher.command_scheduler import (  # noqa: E402
    BatchCommandScheduler,
    parse_batch_file,
    parse_batch_text,
)
from src.launcher.server import Server  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="两阶段指令文件路径")
    source.add_argument("--commands", help="内联两阶段指令文本")
    parser.add_argument(
        "--time-unit",
        choices=["s", "t"],
        default="s",
        help="任务阶段裸数字默认单位 (默认 s)",
    )
    parser.add_argument("--poll-interval", type=float, default=0.5, help="轮询间隔(秒)")
    parser.add_argument(
        "--start-delay",
        type=float,
        default=None,
        help="准备完成后等待该秒数自动发送开始信号; 缺省时按回车触发",
    )
    parser.add_argument("--check", action="store_true", help="仅解析校验, 不启动服务器")
    parser.add_argument("--keep-alive", action="store_true", help="结束后保持服务器运行")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.file and not args.commands:
        print("请提供 --file 或 --commands")
        return 2

    source = args.file or args.commands
    plan = (
        parse_batch_file(args.file, args.time_unit)
        if args.file
        else parse_batch_text(args.commands, args.time_unit)
    )
    print(f"== 解析完成: 准备阶段 {len(plan.prepare)} 条, 任务阶段 {len(plan.task)} 条 ==")
    for command in plan.prepare:
        print(f"  [准备] {command}")
    for entry in plan.task:
        print(f"  [任务] L{entry.line_no}: [{entry.time:g}{entry.unit}] {entry.command}")

    if args.check:
        print("--check 通过: 格式合法, 未启动服务器")
        return 0

    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    try:
        print("== 启动 Minecraft 服务器 ==")
        server.start()
        print(f"服务器已就绪 (pid={server.pid})")

        scheduler = BatchCommandScheduler(
            server,
            plan,
            poll_interval=args.poll_interval,
        )

        print("== 执行准备阶段环境指令 ==")
        prepare_result = scheduler.run_prepare()
        for command, response in prepare_result.prepared:
            print(f"  PREP {command} -> {response!r}")
        for command, error in prepare_result.prepare_errors:
            print(f"  PREP-FAIL {command}: {error}")

        print("== 等待任务开始信号 ==")
        if args.start_delay is None:
            try:
                input("按回车发送任务开始信号 ...\n")
            except EOFError:
                print("无法读取输入, 请改用 --start-delay 自动触发")
                return 1
            scheduler.signal_task_start()
        else:
            print(f"将在 {args.start_delay:g}s 后自动发送任务开始信号")
            time.sleep(args.start_delay)
            scheduler.signal_task_start()
        print("已收到开始信号, 开始计时并执行任务指令")

        result = scheduler.run_task()
        print("== 任务阶段完成 ==")
        print(
            f"任务指令 {result.total}, 已发送 {len(result.sent)}, "
            f"失败 {len(result.errors)}, 取消 {result.cancelled}"
        )
        for entry, response in result.sent:
            print(
                f"  OK L{entry.line_no} [{entry.time:g}{entry.unit}] "
                f"{entry.command} -> {response!r}"
            )
        for entry, error in result.errors:
            print(f"  FAIL L{entry.line_no} {entry.command}: {error}")
        return 0 if not result.errors else 1
    finally:
        if not args.keep_alive:
            server.stop()
        else:
            print(f"--keep-alive: 服务器保持运行 (pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
