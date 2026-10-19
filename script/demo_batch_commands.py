#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量指令执行测试 demo: 按时间顺序向服务器发送指令。

指令格式见 src/launcher/command_scheduler.py 模块说明。

用法:
  python script/demo_batch_commands.py --file script/batch_commands_example.txt
  python script/demo_batch_commands.py --commands "say hi
3s say three"
  python script/demo_batch_commands.py --file x.txt --check   # 仅解析校验
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.launcher.command_scheduler import parse_batch_file, parse_batch_text  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="批次指令文件路径")
    source.add_argument("--commands", help="内联多行批次指令文本")
    parser.add_argument(
        "--time-unit",
        choices=["s", "t"],
        default="s",
        help="裸数字默认单位 (默认 s)",
    )
    parser.add_argument("--poll-interval", type=float, default=0.5, help="轮询间隔(秒)")
    parser.add_argument("--check", action="store_true", help="仅解析校验, 不启动服务器")
    parser.add_argument("--keep-alive", action="store_true", help="批次完成后保持服务器运行")
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
    if plan.prepare:
        print("  准备阶段 (立即执行):")
        for command in plan.prepare:
            print(f"    - {command}")
    print("  任务阶段 (收到开始信号后计时):")
    for entry in plan.task:
        print(f"  L{entry.line_no}: [{entry.time:g}{entry.unit}] {entry.command}")

    if args.check:
        print("--check 通过: 格式合法, 未启动服务器")
        return 0

    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    try:
        print("== 启动 Minecraft 服务器 ==")
        server.start()
        print(f"服务器已就绪 (pid={server.pid})")

        print("== 执行批量指令 ==")
        result = server.run_batch(
            source,
            default_unit=args.time_unit,
            poll_interval=args.poll_interval,
        )

        print("== 批量执行完成 ==")
        print(
            f"任务指令 {result.total}, 已发送 {len(result.sent)}, "
            f"失败 {len(result.errors)}, 准备失败 {len(result.prepare_errors)}, "
            f"已开始 {result.started}, 取消 {result.cancelled}"
        )
        for command, response in result.prepared:
            print(f"  PREP {command} -> {response!r}")
        for command, error in result.prepare_errors:
            print(f"  PREP-FAIL {command}: {error}")
        for entry, response in result.sent:
            print(
                f"  OK L{entry.line_no} [{entry.time:g}{entry.unit}] "
                f"{entry.command} -> {response!r}"
            )
        for entry, error in result.errors:
            print(f"  FAIL L{entry.line_no} {entry.command}: {error}")
        return 0 if not (result.errors or result.prepare_errors) else 1
    finally:
        if not args.keep_alive:
            server.stop()
        else:
            print(f"--keep-alive: 服务器保持运行 (pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
