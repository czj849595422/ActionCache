#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方块技能测试（block.break/place/inspect 的拾取开关与距离开关）。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent；
  2. 通过 RCON 给 agent 16 个圆石（cobblestone）；
  3. 依次验证：
     - 拾取开关：放置圆石 -> 默认 collect_drops=true 破坏 -> 掉落物回收、背包数量恢复；
       collect_drops=false 破坏 -> 不回收、背包数量不变、collection_supported=false；
     - 距离开关：distance_check_enabled=true + max_distance 过小 -> block_out_of_range；
       默认关闭 -> 远距离 inspect 正常执行；
     - plugin 能力：/skill/list 中 mineflayer-collectblock 可用；
  4. 清理：disconnect agent -> 优雅停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_block_skills.py

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


def inventory_count(payload, item_name: str) -> int:
    return sum(
        item.get("count", 0)
        for item in (payload.get("inventory_items") or [])
        if item and item.get("name") == item_name
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565, help="Minecraft 服务器端口")
    parser.add_argument("--bridge-port", type=int, default=3252, help="agent bridge 端口")
    parser.add_argument("--wait-server", type=int, default=180, help="等待服务器就绪超时(秒)")
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None

    try:
        print("== 1/5 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)
        agent = SkillAgent(
            username="block_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        skills = agent._get_bridge("/skill/list")
        capabilities = skills.get("capabilities", {})
        assert capabilities.get("mineflayer-collectblock"), f"collectblock 能力缺失: {capabilities}"
        print("== 2/5 collectblock 插件能力 OK ==")

        print("== 3/5 准备测试场地（发放圆石并寻找可放置位置） ==")
        server.send_command("give block_test cobblestone 16")
        server.send_command("give block_test wooden_pickaxe 1")
        time.sleep(2)
        equipped = planner.execute("inventory.equip", {"name": "wooden_pickaxe", "destination": "hand"})
        assert equipped.ok, f"装备木镐失败: {error_code(equipped)}"
        print("木镐已装备")
        status = agent.get_status()
        pos = status.get("position") or {}
        x, y, z = (
            math.floor(pos.get("x", 0)),
            math.floor(pos.get("y", 0)),
            math.floor(pos.get("z", 0)),
        )
        print(f"agent 位置: ({x}, {y}, {z})")

        candidates = [
            {"x": x + 1, "y": y, "z": z},
            {"x": x - 1, "y": y, "z": z},
            {"x": x, "y": y, "z": z + 1},
            {"x": x, "y": y, "z": z - 1},
        ]
        # 排除 bot 自身所在的方块（服务器不允许在玩家脚下放置）
        candidates = [
            c for c in candidates
            if not (c["x"] == x and c["y"] == y and c["z"] == z)
        ]
        target = None
        for candidate in candidates:
            r = planner.execute("block.inspect", candidate)
            if r.ok:
                payload = r.result
                if payload.get("is_air"):
                    target = candidate
                    break
        assert target is not None, "未找到可放置的空气位置"
        print(f"目标位置: {target}")

        print("== 4/5 执行方块用例 ==")
        # 1) 放置圆石
        r = planner.execute("block.place", {"item": "cobblestone", **target})
        assert r.ok, f"block.place 失败: ok={r.ok}, error_code={error_code(r)}, payload={r.result}"
        placed_payload = r.result
        count_after_place = inventory_count(placed_payload, "cobblestone")
        print(f"  block.place OK, 圆石 {count_after_place}（放置前应少 1）")

        # 2) 默认拾取破坏（collect_drops 默认 true）
        r = planner.execute("block.break", target)
        assert r.ok, f"block.break(collect) 失败: ok={r.ok}, error_code={error_code(r)}"
        payload = r.result
        assert payload.get("collect_drops") is True, f"collect_drops 默认应为 True: {payload}"
        assert payload.get("collection_supported") is True, f"collection_supported 应为 True: {payload}"
        count_after_break = inventory_count(payload, "cobblestone")
        assert count_after_break == count_after_place + 1, (
            f"拾取后圆石数量应恢复为 {count_after_place + 1}, 实际 {count_after_break}, "
            f"collected_count={payload.get('collected_count')}"
        )
        print(
            f"  block.break 默认拾取 OK: collected_count={payload.get('collected_count')}, "
            f"圆石 {count_after_break}"
        )

        # 3) 关闭拾取破坏（collect_drops=false）
        r = planner.execute("block.place", {"item": "cobblestone", **target})
        assert r.ok, f"第二次 block.place 失败: {error_code(r)}"
        count_before_no_collect = inventory_count(r.result, "cobblestone")
        r = planner.execute("block.break", {"collect_drops": False, **target})
        assert r.ok, f"block.break(no collect) 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("collection_supported") is False, f"collection_supported 应为 False: {payload}"
        count_no_collect = inventory_count(payload, "cobblestone")
        assert count_no_collect == count_before_no_collect, (
            f"不拾取时圆石数量应保持 {count_before_no_collect}, 实际 {count_no_collect}"
        )
        print(f"  block.break collect_drops=false OK: 圆石保持 {count_no_collect}")

        # 4) 距离开关：开启 + max_distance 过小 -> block_out_of_range
        far_target = {"x": x + 10, "y": y, "z": z}
        r = planner.execute(
            "block.inspect",
            {"distance_check_enabled": True, "max_distance": 5, **far_target},
        )
        assert not r.ok and error_code(r) == "block_out_of_range", (
            f"距离开关预期 block_out_of_range, 实际 {error_code(r)}"
        )
        print("  block.inspect distance_check_enabled=True -> block_out_of_range OK")

        # 5) 距离开关：默认关闭 -> 远距离 inspect 正常
        r = planner.execute("block.inspect", far_target)
        assert r.ok, f"距离开关闭时 inspect 应成功: {error_code(r)}"
        print(f"  block.inspect 默认（距离开关闭）OK: block={r.result.get('block', {}).get('name')}")

        print("== 5/5 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
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
