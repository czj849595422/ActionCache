#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""farm.harvest 测试（成熟判定 / 数量 / 全部 / 错误 / 中断）。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent（和平模式、禁刷怪）；
  2. 在 bot 附近铺设 3 株成熟小麦 + 1 株未成熟小麦；
  3. 验证：
     - harvest count=2 -> reason=count_reached, harvested=2；
     - harvest 不指定数量 -> reason=all_harvested，收完剩余 1 株成熟小麦，
       未成熟小麦保持原样；
     - 缺少 crop -> invalid_params；范围内无成熟作物 -> no_mature_crop；
     - 持续动作可中断：start 后 stop -> interrupted；
  4. 清理：移除测试方块、disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_farm.py

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


def final_result(st):
    payload = st.result if isinstance(st.result, dict) else {}
    nested = payload.get("result")
    return nested if isinstance(nested, dict) else payload


def wait_terminal(planner, action_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = planner.status(action_id)
        if st.status in ("completed", "failed", "interrupted", "stopped"):
            return st
        time.sleep(1)
    raise TimeoutError(f"action {action_id} 未在 {timeout}s 内结束")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port", type=int, default=3260)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None
    cleaned: list[dict[str, int]] = []

    try:
        print("== 1/6 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)
        agent = SkillAgent(
            username="farm_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        print("== 2/6 铺设测试农田（3 成熟 + 1 未成熟小麦） ==")
        status = agent.get_status()
        pos = status.get("position") or {}
        bx = math.floor(pos.get("x", 0))
        by = math.floor(pos.get("y", 0))
        bz = math.floor(pos.get("z", 0))
        mature = [{"x": bx + 2, "y": by, "z": bz}, {"x": bx + 3, "y": by, "z": bz}, {"x": bx + 4, "y": by, "z": bz}]
        immature = {"x": bx + 5, "y": by, "z": bz}
        for crop_pos in mature + [immature]:
            server.send_command(f"setblock {crop_pos['x']} {crop_pos['y'] - 1} {crop_pos['z']} farmland")
        for crop_pos in mature:
            server.send_command(f"setblock {crop_pos['x']} {crop_pos['y']} {crop_pos['z']} minecraft:wheat[age=7]")
        server.send_command(f"setblock {immature['x']} {immature['y']} {immature['z']} minecraft:wheat[age=0]")
        cleaned.extend([{"x": c["x"], "y": c["y"], "z": c["z"]} for c in mature])
        cleaned.append(immature)
        time.sleep(2)
        print(f"成熟小麦 x{len(mature)} @ y={by}，未成熟 1 株")

        print("== 3/6 harvest count=2 ==")
        r = planner.start("farm.harvest", {"crop": "wheat", "count": 2, "range": 10})
        assert r.ok, f"harvest start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("reason") == "count_reached", (
            f"应 count_reached: status={st.status}, result={payload}"
        )
        assert payload.get("harvested") == 2, f"harvested 应为 2: {payload}"
        print(f"  harvest count=2 OK: reason={payload.get('reason')}, harvested={payload.get('harvested')}")

        print("== 4/6 harvest 不指定数量（全部） ==")
        r = planner.start("farm.harvest", {"crop": "wheat", "range": 10})
        assert r.ok, f"harvest(all) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("reason") == "all_harvested", (
            f"应 all_harvested: status={st.status}, result={payload}"
        )
        assert payload.get("harvested") == 1, f"剩余成熟小麦应为 1 株: {payload}"
        print(f"  harvest all OK: reason={payload.get('reason')}, harvested={payload.get('harvested')}")

        r = planner.execute("block.inspect", immature)
        assert r.ok and r.result.get("block", {}).get("name") == "wheat", (
            f"未成熟小麦应保留: {r.result}"
        )
        print("  未成熟小麦未被收获 OK")

        print("== 5/6 错误路径 ==")
        r = planner.start("farm.harvest", {"range": 10})
        assert not r.ok and error_code(r) == "invalid_params", (
            f"缺少 crop 预期 invalid_params, 实际 {error_code(r)}"
        )
        print("  缺少 crop -> invalid_params OK")

        r = planner.start("farm.harvest", {"crop": "wheat", "range": 10})
        assert not r.ok and error_code(r) == "no_mature_crop", (
            f"无成熟作物预期 no_mature_crop, 实际 {error_code(r)}"
        )
        print("  范围内无成熟作物 -> no_mature_crop OK")

        print("== 6/6 中断 ==")
        server.send_command(f"setblock {immature['x']} {immature['y']} {immature['z']} minecraft:wheat[age=7]")
        time.sleep(1)
        r = planner.start("farm.harvest", {"crop": "wheat", "count": 5, "range": 10})
        assert r.ok, f"harvest(interrupt) start 失败: {error_code(r)}"
        time.sleep(0.5)
        stopped = planner.stop(r.action_id, reason="test_stop")
        assert stopped.ok and stopped.status == "interrupted", (
            f"stop 应 interrupted: status={stopped.status}, error={error_code(stopped)}"
        )
        print("  harvest 中断 OK: status=interrupted")

        print("== 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            for pos2 in cleaned:
                try:
                    server.send_command(f"setblock {pos2['x']} {pos2['y']} {pos2['z']} air")
                    server.send_command(f"setblock {pos2['x']} {pos2['y'] - 1} {pos2['z']} air")
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 {pos2} 失败: {exc}")
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
