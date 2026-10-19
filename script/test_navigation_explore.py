#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""navigation.explore 测试（通用探索 / 定向搜寻 / record / 参数校验）。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent（和平模式、禁刷怪）；
  2. 通用探索：direction=east、duration=5、type=record -> 应正常移动并返回 discoveries；
  3. 定向搜寻：在 bot 前方放置 diamond_ore，target=diamond_ore、type=first ->
     应 reason=target_found 且 found 含 diamond_ore；
  4. record 模式：target=diamond_ore、type=record、duration=5 -> reason=duration_reached，
     found 去重后仍包含 diamond_ore；
  5. 非法 direction -> invalid_params；
  6. 清理：移除测试方块、disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_navigation_explore.py

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def error_code(result) -> str | None:
    payload = result.result if isinstance(result.result, dict) else {}
    return payload.get("error_code") or getattr(result, "error", None)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port", type=int, default=3259)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None
    test_block: dict[str, int] | None = None

    try:
        print("== 1/6 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)
        agent = SkillAgent(
            username="explore_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        print("== 2/6 通用探索（direction=east, duration=5, record） ==")
        r = planner.execute(
            "navigation.explore",
            {"direction": "east", "duration": 5, "type": "record", "scan_interval": 4},
        )
        assert r.ok, f"通用探索失败: {error_code(r)}"
        payload = r.result
        assert payload.get("blocks_moved", 0) >= 1, f"应至少移动 1 段: {payload}"
        assert payload.get("discoveries"), f"应有沿途发现: {payload}"
        print(
            f"  通用探索 OK: reason={payload.get('reason')}, 移动 {payload.get('blocks_moved')} 段, "
            f"访问 {payload.get('visited_count')} 处"
        )

        print("== 3/6 定向搜寻（放置 diamond_ore 后 target=first） ==")
        status = agent.get_status()
        pos = status.get("position") or {}
        bx = math.floor(pos.get("x", 0))
        by = math.floor(pos.get("y", 0))
        bz = math.floor(pos.get("z", 0))
        test_block = {"x": bx + 6, "y": by, "z": bz}
        server.send_command(
            f"setblock {test_block['x']} {test_block['y']} {test_block['z']} diamond_ore"
        )
        time.sleep(2)
        r = planner.execute(
            "navigation.explore",
            {"target": "diamond_ore", "direction": "east", "duration": 30},
        )
        assert r.ok, f"定向搜寻失败: {error_code(r)}"
        payload = r.result
        assert payload.get("reason") == "target_found", f"应 target_found: {payload}"
        names = [b.get("name") for b in payload.get("found") or []]
        assert "diamond_ore" in names, f"found 应含 diamond_ore: {names}"
        print(f"  定向搜寻 OK: 找到 {names}")

        print("== 4/6 record 模式（target=diamond_ore, type=record） ==")
        r = planner.execute(
            "navigation.explore",
            {"target": "diamond_ore", "type": "record", "direction": "east", "duration": 5},
        )
        assert r.ok, f"record 模式失败: {error_code(r)}"
        payload = r.result
        assert payload.get("reason") == "duration_reached", f"record 应到点返回: {payload}"
        names = [b.get("name") for b in payload.get("found") or []]
        assert "diamond_ore" in names, f"record 应记录 diamond_ore: {names}"
        print("  record 模式 OK: reason=duration_reached, found 去重后含 diamond_ore")

        print("== 5/6 参数校验 ==")
        r = planner.execute("navigation.explore", {"direction": "diagonal"})
        assert not r.ok and error_code(r) == "invalid_params", (
            f"非法 direction 预期 invalid_params, 实际 {error_code(r)}"
        )
        print("  非法 direction -> invalid_params OK")

        print("== 6/6 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            if test_block:
                try:
                    server.send_command(
                        f"setblock {test_block['x']} {test_block['y']} {test_block['z']} air"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理测试方块失败: {exc}")
            if agent:
                try:
                    agent.disconnect()
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 agent 失败: {exc}")
            server.stop()
            time.sleep(1)
        else:
            print(f"[test] --keep-alive: 保留进程 (server pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
