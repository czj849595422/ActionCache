#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""熔炉技能测试：自动打开 + smelt_item（wait/count） + 数量默认 1。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent（和平模式、禁刷怪）；
  2. RCON 放置 1 个熔炉并发放 coal / iron_ore；
  3. 验证：
     - put_fuel 无需先 open（自动打开），随后 close 正常；
     - smelt_item wait=true count=1：等待烧完并取出产物，熔炉自动关闭；
     - put_input 不传 count：默认放置 1 个；
     - smelt_item wait=false count=2：只配置燃料/原料并立即关闭，
       稍后重新打开可取到已烧出的产物；
  4. 清理：移除熔炉、disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_furnace_smelt.py

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
    parser.add_argument("--bridge-port", type=int, default=3256)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None
    furnace_pos: dict[str, int] | None = None

    try:
        print("== 1/6 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)
        agent = SkillAgent(
            username="furnace_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        print("== 2/6 放置熔炉并发放物品 ==")
        status = agent.get_status()
        pos = status.get("position") or {}
        bx = math.floor(pos.get("x", 0))
        by = math.floor(pos.get("y", 0))
        bz = math.floor(pos.get("z", 0))
        furnace_pos = {"x": bx + 2, "y": by, "z": bz}
        server.send_command(f"setblock {bx + 2} {by} {bz} furnace")
        server.send_command("give furnace_test coal 8")
        server.send_command("give furnace_test iron_ore 4")
        time.sleep(2)
        print(f"熔炉位置: {furnace_pos}")

        print("== 3/6 put_fuel 自动打开 ==")
        r = planner.execute(
            "furnace.put_fuel",
            {"item": "coal", "count": 1, "position": furnace_pos},
        )
        assert r.ok, f"put_fuel(自动打开) 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("placed") and payload["placed"][0].get("placed") == 1, payload
        print("  put_fuel 无需 open 即自动打开并放置 1 个煤炭 OK")

        r = planner.execute("furnace.close", {})
        assert r.ok and r.result.get("closed"), f"close 失败: {r.result}"
        print("  close OK")

        print("== 4/6 smelt_item wait=true count=1 ==")
        r = planner.execute(
            "furnace.smelt_item",
            {"item": "iron_ore", "fuel": "coal", "count": 1, "wait": True, "position": furnace_pos},
        )
        assert r.ok, f"smelt_item(wait) 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("waited") is True, payload
        assert payload.get("smelted_count") == 1, payload
        print(f"  smelt_item wait=true OK: smelted_count={payload.get('smelted_count')}")

        r = planner.execute("furnace.close", {})
        assert r.ok and r.result.get("already_closed"), f"smelt_item 后会话应已关闭: {r.result}"
        print("  smelt_item 结束后熔炉已自动关闭 OK")

        print("== 5/6 put_input 数量默认 1 ==")
        r = planner.execute("furnace.put_input", {"item": "iron_ore", "position": furnace_pos})
        assert r.ok, f"put_input(默认数量) 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("placed") and payload["placed"][0].get("placed") == 1, payload
        print("  put_input 未传 count 默认放置 1 个 OK")
        planner.execute("furnace.close", {})

        print("== 6/6 smelt_item wait=false count=2（不等待） ==")
        r = planner.execute(
            "furnace.smelt_item",
            {"item": "iron_ore", "fuel": "coal", "count": 2, "wait": False, "position": furnace_pos},
        )
        assert r.ok, f"smelt_item(no wait) 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("waited") is False, payload
        assert payload.get("configured_count") == 2, payload
        print(f"  smelt_item wait=false OK: 配置 {payload.get('configured_count')} 个后立即关闭")

        r = planner.execute("furnace.close", {})
        assert r.ok and r.result.get("already_closed"), f"wait=false 后会话应已关闭: {r.result}"
        print("  wait=false 后熔炉已关闭（可执行其他操作）OK")

        print("  等待烧炼完成后取产物 ...")
        time.sleep(13)
        r = planner.execute("furnace.take_output", {"position": furnace_pos})
        assert r.ok, f"稍后 take_output 失败: {error_code(r)}"
        taken = r.result.get("taken") or []
        assert any(i.get("name") == "iron_ingot" for i in taken), f"未取到铁锭: {taken}"
        print(f"  稍后 take_output OK: 取到 {[i.get('name') for i in taken]}")
        planner.execute("furnace.close", {})

        print("== 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            if furnace_pos:
                try:
                    server.send_command(f"setblock {furnace_pos['x']} {furnace_pos['y']} {furnace_pos['z']} air")
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理熔炉失败: {exc}")
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
