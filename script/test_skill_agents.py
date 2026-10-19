#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SkillAgent 全链路测试：启动 Minecraft 服务器并登录 4 个 agent。

流程：
  1. 启动 src/minecraft 下的服务器（复用 src/launcher/server.py），等待就绪；
 2. 创建 4 个 SkillAgent（各自独立 bridge_skill.js 子进程 + 独占端口），
     逐个 connect() 登录服务器；
 3. 验证每个 agent：/status.spawned、/skill/list 技能数、
     oneshot skill（inventory.inspect）与 continuous skill（navigation.follow_position
     start -> stop，验证中断钩子）均正常；
 4. 验证通过后默认进入保活模式：服务端与 4 个 agent 保持运行，
    按 Ctrl+C（或 --duration 到期）统一清理：disconnect 4 个 agent
    -> 停止各自 bridge 进程 -> 优雅停止服务器，避免僵尸进程。
"""

# 运行：
#   默认保活（Ctrl+C 结束并清理）：
#     & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_skill_agents.py
#   验证后立即清理：
#     & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_skill_agents.py --no-keep-alive

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565, help="Minecraft 服务器端口")
    parser.add_argument(
        "--bridge-port-start", type=int, default=3201,
        help="第一个 agent 的 bridge 端口（之后递增）",
    )
    parser.add_argument("--agent-count", type=int, default=4, help="登录 agent 数量")
    parser.add_argument("--wait-server", type=int, default=180, help="等待服务器就绪超时(秒)")
    parser.add_argument(
        "--keep-alive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="验证通过后保持运行 (默认)；--no-keep-alive 则验证完立即清理",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="保活时长(秒)，0 表示直到 Ctrl+C (默认)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agents: list[SkillAgent] = []

    try:
        print("== 1/5 启动 Minecraft 服务器 ==")
        server.start()
        print(f"服务器已就绪 (pid={server.pid}, port={args.mc_port})")

        print(f"== 2/5 创建并登录 {args.agent_count} 个 SkillAgent ==")
        for i in range(args.agent_count):
            username = f"bot{i + 1}"
            agent = SkillAgent(
                username=username,
                host="localhost",
                port=args.mc_port,
                bridge_port=args.bridge_port_start + i,
                log_dir=log_dir,
            )
            agents.append(agent)
            data = agent.connect()
            caps = data.get("capabilities", {})
            print(
                f"  {username} 登录成功 (bridge_port={agent.bridge_port}, "
                f"pathfinder={caps.get('mineflayer-pathfinder')})"
            )

        print("== 3/5 逐 agent 验证 skill 链路 ==")
        for agent in agents:
            status = agent.get_status()
            spawned = status.get("spawned")
            skill_list = agent._get_bridge("/skill/list")
            skill_count = len(skill_list.get("skills", {}))

            inv = agent.action_planner.execute("inventory.inspect", {})
            inv_ok = bool(inv.ok and inv.result)

            follow = agent.action_planner.start(
                "navigation.follow_position",
                {
                    "x": status.get("position", {}).get("x", 0),
                    "y": status.get("position", {}).get("y", 64),
                    "z": status.get("position", {}).get("z", 0),
                    "range": 1,
                },
            )
            follow_ok = bool(follow.ok and follow.action_id)
            if follow_ok:
                stopped = agent.action_planner.stop(follow.action_id, reason="test_stop")
                stop_ok = bool(stopped.ok and stopped.status == "interrupted")
            else:
                stop_ok = False

            print(
                f"  {agent.username}: spawned={spawned}, skills={skill_count}, "
                f"inventory.inspect ok={inv_ok}, follow_position start/stop ok={follow_ok}/{stop_ok}"
            )
            assert spawned, f"{agent.username} 未 spawn"
            assert skill_count >= 12, f"{agent.username} 技能加载异常: {skill_count}"
            assert inv_ok, f"{agent.username} inventory.inspect 失败: {inv.raw}"
            assert follow_ok and stop_ok, (
                f"{agent.username} follow_position 生命周期失败: "
                f"start={follow.raw}, stop={stopped.raw if follow_ok else None}"
            )

        print("== 4/5 全部验证通过 ==")
        if not args.keep_alive:
            print("--no-keep-alive: 立即清理")
            return 0

        print(
            f"== 5/5 保活模式：{len(agents)} 个 agent 与服务器保持运行，"
            "Ctrl+C 或 --duration 到期后统一清理 =="
        )
        deadline = None if args.duration <= 0 else time.time() + args.duration
        last_heartbeat = time.time()
        while True:
            if deadline is not None and time.time() >= deadline:
                print(f"[test] --duration {args.duration}s 到期，开始清理")
                break
            dead = [a.username for a in agents if not a.is_bridge_running()]
            if dead or not server.is_running:
                print(
                    f"[test] 检测到进程退出: bridges_dead={dead}, "
                    f"server_running={server.is_running}，结束并清理"
                )
                return 1
            if time.time() - last_heartbeat >= 30:
                print(
                    f"[test] 心跳: server=运行, "
                    f"bridges={len(agents) - len(dead)}/{len(agents)}"
                )
                last_heartbeat = time.time()
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("\n[test] 收到 Ctrl+C，开始清理")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        for agent in reversed(agents):
            try:
                agent.disconnect()
            except Exception as exc:  # noqa: BLE001
                print(f"[test] 清理 {agent.username} 失败: {exc}")
        server.stop()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
