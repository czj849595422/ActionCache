#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署服务器 demo 编排: 启动 Minecraft 服务器并默认无限保持运行。

- 默认不设时长上限, 持续运行直到手动停止 (Ctrl+C 或 --duration)。
- 服务器意外退出时 demo 自动结束并提示。
- 退出时调用 Server.stop() 优雅存档关闭。
"""

import argparse
import sys
import time
from pathlib import Path

from .server import Server

ROOT = Path(__file__).resolve().parent.parent.parent


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="运行时长(秒), 0 表示无限 (默认)",
    )
    parser.add_argument(
        "--server-dir",
        default=None,
        help="服务器目录 (默认 src/minecraft)",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="结束后保留服务器进程(调试用)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=args.server_dir, log_dir=log_dir)

    try:
        print("[demo] ===== 部署 Minecraft 服务器 =====")
        server.start()
        duration_desc = f"{args.duration:.0f}s" if args.duration > 0 else "无限"
        print(
            f"[demo] 服务器已就绪并持续运行 (pid={server.pid}, 时长: {duration_desc}), "
            "Ctrl+C 停止"
        )

        deadline = None if args.duration <= 0 else time.time() + args.duration
        while True:
            if not server.is_running:
                print("[demo] 服务器进程已退出, demo 结束")
                return 1
            if deadline is not None and time.time() >= deadline:
                print(f"[demo] 达到运行时长 {args.duration:.0f}s, 开始停止服务器")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("[demo] 收到中断, 开始停止服务器")
    finally:
        if not args.keep_alive:
            server.stop()
        else:
            print(f"[demo] --keep-alive: 服务器保持运行 (pid={server.pid})")

    print("[demo] ===== 部署完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
