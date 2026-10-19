#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo 编排逻辑: 启动 Minecraft 服务器并部署一个 Mineflayer agent。

流程:
  1. 启动 src/minecraft 下的 1.19 服务器 (java -jar server.jar)
  2. 等待服务器就绪 ("Done ...")
  3. 启动 src/env/mineflayer/index_single_bot.js (单 agent 桥)
  4. POST /start 部署 agent (默认用户名 test_agent, 对应 ops.json 中的 OP)
  5. POST /step 执行一步动作 (聊天 + 传送), 打印观测结果
  6. 清理: 停 bot -> 停桥 -> 优雅停止服务器 (向 stdin 发送 stop)

前置:
  cd src/env/mineflayer && npm install --ignore-scripts
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from .utils import ManagedProcess, post_json


def find_status_position(obs):
    pos = {}
    for event_name, result in obs:
        if isinstance(result, dict) and "status" in result:
            # 取最后一个 status 事件, 即步骤结束时的最终位置
            pos = result["status"].get("position") or {}
    return pos


def summarize_obs(obs, label):
    print(f"[demo] ---- {label} 观测摘要 ----")
    if not isinstance(obs, list):
        print(f"[demo] 无法解析观测数据: {obs!r}")
        return
    for event_name, result in obs:
        if not isinstance(result, dict):
            continue
        if "status" in result:
            st = result["status"]
            pos = st.get("position") or {}
            print(
                f"[demo] status: hp={st.get('health')} food={st.get('food')} "
                f"pos=({pos.get('x')}, {pos.get('y')}, {pos.get('z')}) "
                f"inventoryUsed={st.get('inventoryUsed')} biome={st.get('biome')}"
            )
        if "onChat" in result:
            chat = result["onChat"]
            if isinstance(chat, str) and chat:
                print(f"[demo] chat: {chat}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565, help="Minecraft 服务器端口")
    parser.add_argument("--http-port", type=int, default=3000, help="桥接 HTTP 端口")
    parser.add_argument("--username", default="test_agent", help="agent 用户名 (默认 test_agent, 对应 ops.json OP)")
    parser.add_argument("--wait-server", type=int, default=180, help="等待服务器就绪超时(秒)")
    parser.add_argument("--keep-alive", action="store_true", help="结束后保留进程(调试用)")
    return parser


def main():
    args = build_parser().parse_args()

    server_dir = ROOT / "src" / "minecraft"
    bridge_dir = ROOT / "src" / "env" / "mineflayer"
    bridge_script = bridge_dir / "index_single_bot.js"
    log_dir = ROOT / "script" / "logs"

    if not (server_dir / "server.jar").exists():
        sys.exit(f"[demo] 未找到服务器: {server_dir / 'server.jar'}")
    if not (bridge_dir / "node_modules" / "mineflayer").exists():
        sys.exit(
            "[demo] 缺少 node_modules, 请先执行: "
            f"cd {bridge_dir} && npm install --ignore-scripts"
        )
    if not bridge_script.exists():
        sys.exit(f"[demo] 未找到桥接脚本: {bridge_script}")

    procs = []
    try:
        print("[demo] ===== 简易 Demo: 启动 Minecraft 服务器并部署 agent =====")

        print("[demo] 1/5 启动 Minecraft 服务器 ...")
        server = ManagedProcess(
            "mc-server",
            ["java", "-Xms2G", "-Xmx2G", "-jar", "server.jar", "nogui"],
            server_dir,
            ready_pattern=r"Done \([0-9.]+s\)! For help",
            fail_patterns=[
                r"BindException",
                r"Address already in use",
                r"Failed to load properties",
                r"A problem occurred running the server",
                r"Error: Could not find or load main class",
            ],
            log_dir=log_dir,
        )
        server.start()
        procs.append(server)
        server.wait_ready(args.wait_server)
        print("[demo] Minecraft 服务器已就绪")

        print("[demo] 2/5 启动 Mineflayer 桥接 ...")
        bridge = ManagedProcess(
            "bridge",
            ["node", str(bridge_script), str(args.http_port)],
            ROOT,
            ready_pattern=rf"Server started on port {args.http_port}",
            fail_patterns=[r"Cannot find module", r"Error:"],
            log_dir=log_dir,
        )
        bridge.start()
        procs.append(bridge)
        bridge.wait_ready(60)
        print("[demo] 桥接已就绪")

        print(f"[demo] 3/5 部署 agent ({args.username}) ...")
        status, data = post_json(
            f"http://127.0.0.1:{args.http_port}/start",
            {"port": args.mc_port, "username": args.username, "waitTicks": 5, "spread": False},
            timeout=120,
        )
        if status != 200:
            raise RuntimeError(f"/start 返回 {status}: {data}")
        obs_start = json.loads(data["bot"])
        summarize_obs(obs_start, "部署后")

        print("[demo] 4/5 执行一步动作 (聊天 + 传送 3 格) ...")
        code = (
            'bot.chat("Hello from ActionCache demo agent");'
            "await bot.waitForTicks(10);"
            'bot.chat("/tp @s ~ ~ ~3");'
            "await bot.waitForTicks(20)"
        )
        status, data = post_json(
            f"http://127.0.0.1:{args.http_port}/step",
            {"code": code, "programs": ""},
            timeout=120,
        )
        if status != 200:
            raise RuntimeError(f"/step 返回 {status}: {data}")
        obs_after = json.loads(data["bot"])
        summarize_obs(obs_after, "动作后")

        pos_start = find_status_position(obs_start)
        pos_end = find_status_position(obs_after)
        if pos_start and pos_end:
            dx = pos_end.get("x", 0) - pos_start.get("x", 0)
            dz = pos_end.get("z", 0) - pos_start.get("z", 0)
            print(f"[demo] 位移: ({dx:.2f}, {dz:.2f}) 方块 (水平)")

        print("[demo] 5/5 清理 ...")
    except Exception as exc:  # noqa: BLE001
        print(f"[demo] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            for proc in reversed(procs):
                try:
                    if proc.name == "mc-server" and proc.is_running:
                        proc.send_line("stop")
                        time.sleep(2)
                    proc.stop()
                except Exception as exc:  # noqa: BLE001
                    print(f"[demo] 清理 {proc.name} 失败: {exc}")

    print("[demo] ===== Demo 完成 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
