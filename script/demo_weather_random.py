#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天气随机切换测试 demo: 每 10 秒通过 mcrcon 随机发送天气指令并打印效果。

- 项目规则: 测试 demo 放在 script 下; 本脚本只做调用与演示逻辑,
  不定义模块/类, 服务器操作统一走 src 下的 Server (mcrcon 通道)。
- 指令取自 docs/Minecraft指令速查卡.md 的"时间 & 天气"章节。
- 默认无限运行 (Ctrl+C 停止), 可用 --duration 限时。

用法:
  python script/demo_weather_random.py              # 每 10 秒随机换天气
  python script/demo_weather_random.py --duration 60
  python script/demo_weather_random.py --interval 5
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from launcher.server import Server  # noqa: E402

parser = argparse.ArgumentParser(description="天气随机切换测试 demo")
parser.add_argument("--duration", type=float, default=0, help="运行时长(秒), 0=无限 (默认)")
parser.add_argument("--interval", type=float, default=10, help="天气更换间隔(秒), 默认 10")
parser.add_argument("--server-dir", default=None, help="服务器目录 (默认 src/minecraft)")
parser.add_argument("--keep-alive", action="store_true", help="结束后保留服务器进程(调试用)")
args = parser.parse_args()

ROOT = Path(__file__).resolve().parent.parent
server = Server(server_dir=args.server_dir, log_dir=ROOT / "script" / "logs")

WEATHER_COMMANDS = [
    "weather clear",
    "weather rain",
    "weather thunder",
]

try:
    print(f"[demo] ===== 天气随机切换 Demo (每 {args.interval:.0f}s) =====")
    server.start()
    print(f"[demo] 服务器已就绪 (pid={server.pid})")

    print("[demo] 关闭自然天气循环: gamerule doWeatherCycle false")
    print(
        "[demo]   响应:",
        server.send_command("gamerule doWeatherCycle false", response_timeout=10),
    )

    deadline = None if args.duration <= 0 else time.time() + args.duration
    while True:
        if not server.is_running:
            print("[demo] 服务器进程已退出, demo 结束")
            sys.exit(1)
        if deadline is not None and time.time() >= deadline:
            print(f"[demo] 达到运行时长 {args.duration:.0f}s, 开始停止服务器")
            break

        command = random.choice(WEATHER_COMMANDS)
        print(f"[demo] [{time.strftime('%H:%M:%S')}] 发送: {command}")
        print(f"[demo]   响应: {server.send_command(command, response_timeout=10)!r}")
        time.sleep(args.interval)
except KeyboardInterrupt:
    print("[demo] 收到中断, 开始停止服务器")
finally:
    if not args.keep_alive:
        server.stop()
    else:
        print(f"[demo] --keep-alive: 服务器保持运行 (pid={server.pid})")

print("[demo] ===== Demo 完成 =====")
sys.exit(0)
